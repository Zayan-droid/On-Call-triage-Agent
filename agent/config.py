"""Central, env-driven configuration.

Design note: every knob the agent has lives here and is read from the
environment exactly once, at module import time in Lambda (module scope), so a
warm container never re-reads it. Tests and the eval harness build a Config
directly with overrides instead of mutating os.environ, which keeps parallel
test execution safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

# Bedrock model ids. Kept as constants so the eval harness can sweep them.
CLAUDE_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
CLAUDE_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

DEFAULT_MODEL = CLAUDE_SONNET
DEFAULT_JUDGE_MODEL = CLAUDE_SONNET


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration.

    Frozen because a mutable global config is the classic source of
    "works in test, breaks in Lambda" bugs when a warm container carries
    state between invocations.
    """

    region: str = "us-east-1"
    table_name: str = "triage-agent"
    sns_topic_arn: str = ""
    model_id: str = DEFAULT_MODEL
    judge_model_id: str = DEFAULT_JUDGE_MODEL
    prompt_variant: str = "v2_investigate_first"

    # Dedupe window for create_incident. 15 min is the spec's requirement.
    dedupe_window_min: int = 15
    # Hard ceiling on the Converse tool-use loop. Prevents an infinite
    # model/tool ping-pong from burning the Lambda timeout and the token budget.
    # 8 leaves room for the five tools plus a retry after a validation error and
    # a final verdict turn; a full investigation uses six.
    max_tool_iterations: int = 8
    max_tokens: int = 2048
    # Temperature 0 so the deterministic scorers actually measure the prompt,
    # not sampling noise. See docs/DESIGN_DECISIONS.md D-14.
    temperature: float = 0.0

    incident_ttl_days: int = 30
    metrics_namespace: str = "OncallTriage"
    service_metrics_namespace: str = "OncallTriage/Services"

    # dry_run short-circuits every outward-facing side effect (SNS publish).
    # The eval harness runs with dry_run=True by default so a 38-case sweep
    # does not send 38 emails.
    dry_run: bool = False
    api_key: str = ""

    # Bounds used to validate tool arguments before they reach AWS.
    min_window_minutes: int = 5
    max_window_minutes: int = 1440
    max_deploy_lookback_hours: int = 168
    max_datapoints_returned: int = 60

    @classmethod
    def from_env(cls, **overrides: Any) -> Config:
        cfg = cls(
            region=_env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1")),
            table_name=_env("TRIAGE_TABLE_NAME", "triage-agent"),
            sns_topic_arn=_env("TRIAGE_SNS_TOPIC_ARN", ""),
            model_id=_env("TRIAGE_MODEL_ID", DEFAULT_MODEL),
            judge_model_id=_env("TRIAGE_JUDGE_MODEL_ID", DEFAULT_JUDGE_MODEL),
            prompt_variant=_env("TRIAGE_PROMPT_VARIANT", "v2_investigate_first"),
            dedupe_window_min=_env_int("TRIAGE_DEDUPE_WINDOW_MIN", 15),
            max_tool_iterations=_env_int("TRIAGE_MAX_TOOL_ITERATIONS", 8),
            max_tokens=_env_int("TRIAGE_MAX_TOKENS", 2048),
            temperature=_env_float("TRIAGE_TEMPERATURE", 0.0),
            incident_ttl_days=_env_int("TRIAGE_INCIDENT_TTL_DAYS", 30),
            metrics_namespace=_env("TRIAGE_METRICS_NAMESPACE", "OncallTriage"),
            service_metrics_namespace=_env(
                "TRIAGE_SERVICE_METRICS_NAMESPACE", "OncallTriage/Services"
            ),
            dry_run=_env_bool("TRIAGE_DRY_RUN", False),
            api_key=_env("TRIAGE_API_KEY", ""),
        )
        return replace(cfg, **overrides) if overrides else cfg

    def redacted(self) -> dict:
        """Safe-to-log view. Never log the API key."""
        data = self.__dict__.copy()
        data["api_key"] = "***set***" if self.api_key else "***unset***"
        return data
