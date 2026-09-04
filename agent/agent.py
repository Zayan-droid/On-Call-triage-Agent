"""The Bedrock Converse tool-use loop.

The loop itself is small. What matters is what surrounds it:

* **The decision is read from side effects, not from the model's own report.**
  `TriageResult.paged` is true because a `page_oncall` call succeeded, not
  because the model wrote `"decision": "PAGE"`. Models misreport their own
  behaviour, and an eval that scores self-reports measures the model's honesty
  rather than its judgement. The self-report is still captured, and the gap
  between the two is itself reported as `decision_consistent` -- a free extra
  signal that costs one boolean.

* **The loop always terminates with a decision.** Hitting the iteration ceiling
  is not an error path: we drop the tool config and ask once more for a verdict
  on the evidence already gathered. Without that, a model that keeps requesting
  tools produces a null result, and a null scores as "did not page" -- silently
  inflating the false-negative rate with what is really a harness bug.

* **Bedrock throttling is retried with jitter, everything else is not.**
  Retrying a ValidationException just burns the same error five times.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent import prompts
from agent.config import Config
from agent.obs import log_error, log_info, log_warn
from agent.tools import TOOL_NAMES, ToolCall, ToolContext, dispatch, tool_config

# Bedrock errors that are worth trying again. Anything else is a bug in our
# request and will fail identically on every retry.
RETRYABLE = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
    "ModelNotReadyException",
}

VALID_DECISIONS = ("PAGE", "INCIDENT_ONLY", "NOISE")


@dataclass
class TriageResult:
    """Everything one triage produced. This is what the eval harness scores."""

    alert: dict
    correlation_id: str

    # Ground truth: derived from the trace, i.e. from what actually happened.
    decision: str = "NOISE"
    paged: bool = False
    incident_id: str | None = None
    incident_created: bool = False

    # What the model said it did. Kept separate from the above on purpose.
    self_reported_decision: str | None = None
    decision_consistent: bool = True

    severity: str | None = None
    reasoning: str = ""
    evidence: list[str] = field(default_factory=list)
    runbook_cited: str | None = None
    final_text: str = ""

    trace: list[ToolCall] = field(default_factory=list)
    iterations: int = 0
    stop_reason: str | None = None
    hit_iteration_cap: bool = False
    forced_decision: bool = False

    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    latency_ms: float = 0.0
    model_id: str = ""
    prompt_variant: str = ""

    error: str | None = None
    error_kind: str | None = None

    def tool_names(self) -> list[str]:
        return [c.name for c in self.trace]

    def successful_tool_names(self) -> list[str]:
        return [c.name for c in self.trace if c.ok]

    def to_dict(self) -> dict:
        return {
            "alert": self.alert,
            "correlation_id": self.correlation_id,
            "decision": self.decision,
            "paged": self.paged,
            "incident_id": self.incident_id,
            "incident_created": self.incident_created,
            "self_reported_decision": self.self_reported_decision,
            "decision_consistent": self.decision_consistent,
            "severity": self.severity,
            "reasoning": self.reasoning,
            "evidence": self.evidence,
            "runbook_cited": self.runbook_cited,
            "final_text": self.final_text,
            "trace": [c.to_dict() for c in self.trace],
            "tools_called": self.tool_names(),
            "iterations": self.iterations,
            "stop_reason": self.stop_reason,
            "hit_iteration_cap": self.hit_iteration_cap,
            "forced_decision": self.forced_decision,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_calls": self.model_calls,
            "latency_ms": self.latency_ms,
            "model_id": self.model_id,
            "prompt_variant": self.prompt_variant,
            "error": self.error,
            "error_kind": self.error_kind,
        }


# --------------------------------------------------------------------------
# Parsing the model's final message
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the verdict object out of the final message.

    Three strategies, cheapest first: a whole-string parse, a fenced block, then
    a brace-matching scan. Models wrap JSON in prose or fences often enough that
    handling only the clean case would throw away real answers -- and a failed
    parse here would look identical to "the agent had no opinion", which is the
    single most misleading thing this harness could report.
    """
    if not text:
        return None

    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    match = _FENCE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Brace matching, scanning from the last '{' backwards: the verdict is
    # normally the last thing in the message, after any preamble.
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(starts):
        depth, in_string, escape = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
    return None


def _message_text(message: dict) -> str:
    return "\n".join(
        block["text"] for block in message.get("content", []) if "text" in block
    ).strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


# --------------------------------------------------------------------------
# Bedrock call with bounded retry
# --------------------------------------------------------------------------


def _converse(
    bedrock: Any,
    *,
    model_id: str,
    system: list[dict],
    messages: list[dict],
    inference: dict,
    tools: dict | None,
    max_attempts: int = 4,
    correlation_id: str = "",
    sleeper: Any = time.sleep,
    rng: Any = None,
) -> dict:
    rng = rng or random.Random()
    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "system": system,
        "messages": messages,
        "inferenceConfig": inference,
    }
    if tools:
        kwargs["toolConfig"] = tools

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return bedrock.converse(**kwargs)
        except Exception as exc:
            name = type(exc).__name__
            code = None
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = response.get("Error", {}).get("Code")
            retryable = (code in RETRYABLE) or (name in RETRYABLE)
            last_exc = exc
            if not retryable or attempt == max_attempts:
                raise
            # Exponential backoff with full jitter. Without the jitter, a burst
            # of concurrent eval cases that all throttle would retry in lockstep
            # and throttle again together.
            delay = min(8.0, 0.5 * (2 ** (attempt - 1)))
            delay = rng.uniform(0, delay)
            log_warn(
                "bedrock_retry",
                correlation_id=correlation_id,
                attempt=attempt,
                error_code=code or name,
                sleep_s=round(delay, 3),
            )
            sleeper(delay)
    raise last_exc  # pragma: no cover - unreachable, loop either returns or raises


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def run_triage(
    alert: dict,
    *,
    cfg: Config,
    bedrock: Any,
    ddb: Any = None,
    cw: Any = None,
    sns: Any = None,
    correlation_id: str = "",
    now: datetime | None = None,
    prompt_variant: str | None = None,
    model_id: str | None = None,
    sleeper: Any = time.sleep,
) -> TriageResult:
    """Investigate one alert and return everything that happened."""
    started = time.perf_counter()
    variant = prompt_variant or cfg.prompt_variant
    model = model_id or cfg.model_id

    ctx = ToolContext(
        cfg=cfg,
        alert=alert,
        ddb=ddb,
        cw=cw,
        sns=sns,
        correlation_id=correlation_id,
        now=now,
    )
    result = TriageResult(
        alert=alert,
        correlation_id=correlation_id,
        trace=ctx.trace,
        model_id=model,
        prompt_variant=variant,
    )

    system = [{"text": prompts.get_system_prompt(variant)}]
    messages: list[dict] = [
        {"role": "user", "content": [{"text": prompts.build_user_message(alert)}]}
    ]
    inference = {"maxTokens": cfg.max_tokens, "temperature": cfg.temperature}
    tools = tool_config()

    final_text = ""
    try:
        for iteration in range(1, cfg.max_tool_iterations + 1):
            result.iterations = iteration
            response = _converse(
                bedrock,
                model_id=model,
                system=system,
                messages=messages,
                inference=inference,
                tools=tools,
                correlation_id=correlation_id,
                sleeper=sleeper,
            )
            result.model_calls += 1
            usage = response.get("usage", {}) or {}
            result.input_tokens += int(usage.get("inputTokens", 0) or 0)
            result.output_tokens += int(usage.get("outputTokens", 0) or 0)

            output = response.get("output", {}).get("message")
            if not output:
                raise RuntimeError("Bedrock returned no output message.")
            messages.append(output)
            result.stop_reason = response.get("stopReason")

            text = _message_text(output)
            if text:
                final_text = text

            if result.stop_reason != "tool_use":
                break

            # Execute every tool block in the turn. A model may request several
            # in parallel, and Bedrock requires a toolResult for each requested
            # toolUseId -- missing one is a ValidationException on the next call.
            tool_results = []
            for block in output.get("content", []):
                use = block.get("toolUse")
                if not use:
                    continue
                payload, is_error = dispatch(ctx, use.get("name", ""), use.get("input"))
                entry: dict[str, Any] = {
                    "toolUseId": use.get("toolUseId"),
                    "content": [{"json": payload}],
                }
                if is_error:
                    entry["status"] = "error"
                tool_results.append({"toolResult": entry})

            if not tool_results:
                # stopReason said tool_use but no toolUse block came back. Do not
                # loop forever on a malformed turn.
                log_warn(
                    "tool_use_without_blocks",
                    correlation_id=correlation_id,
                    iteration=iteration,
                )
                break

            messages.append({"role": "user", "content": tool_results})
        else:
            # Fell out of the for loop: the model still wanted tools at the cap.
            result.hit_iteration_cap = True
            log_warn(
                "iteration_cap_reached",
                correlation_id=correlation_id,
                iterations=cfg.max_tool_iterations,
                tools_called=ctx.tool_names(),
            )
            messages.append(
                {"role": "user", "content": [{"text": prompts.FORCED_DECISION_NUDGE}]}
            )
            forced = _converse(
                bedrock,
                model_id=model,
                system=system,
                messages=messages,
                inference=inference,
                tools=None,  # dropping toolConfig forces a text answer
                correlation_id=correlation_id,
                sleeper=sleeper,
            )
            result.model_calls += 1
            result.forced_decision = True
            usage = forced.get("usage", {}) or {}
            result.input_tokens += int(usage.get("inputTokens", 0) or 0)
            result.output_tokens += int(usage.get("outputTokens", 0) or 0)
            forced_message = forced.get("output", {}).get("message", {})
            result.stop_reason = forced.get("stopReason")
            text = _message_text(forced_message)
            if text:
                final_text = text

    except Exception as exc:
        # A model failure must not lose the investigation. Whatever tools already
        # ran are in the trace, the side effects that already happened are real,
        # and the result still describes them accurately.
        result.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        result.error_kind = "model_error"
        log_error(
            "triage_model_error",
            correlation_id=correlation_id,
            error=result.error,
            iterations=result.iterations,
            tools_called=ctx.tool_names(),
        )

    result.final_text = final_text
    _apply_side_effects(result, ctx)
    _apply_self_report(result, final_text)

    result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    log_info(
        "triage_complete",
        correlation_id=correlation_id,
        service=alert.get("service"),
        environment=alert.get("environment", "prod"),
        decision=result.decision,
        paged=result.paged,
        incident_id=result.incident_id,
        tools_called=result.tool_names(),
        iterations=result.iterations,
        model_calls=result.model_calls,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        duration_ms=result.latency_ms,
        decision_consistent=result.decision_consistent,
        error=result.error,
    )
    return result


def _apply_side_effects(result: TriageResult, ctx: ToolContext) -> None:
    """Derive the authoritative decision from the trace."""
    paged = any(c.name == "page_oncall" and c.ok for c in ctx.trace)
    incident_calls = [c for c in ctx.trace if c.name == "create_incident" and c.ok]

    result.paged = paged
    result.incident_created = any(
        (c.result or {}).get("created") for c in incident_calls
    )
    for call in incident_calls:
        incident_id = (call.result or {}).get("incident_id")
        if incident_id:
            result.incident_id = incident_id
            break

    if paged:
        result.decision = "PAGE"
    elif incident_calls:
        result.decision = "INCIDENT_ONLY"
    else:
        result.decision = "NOISE"


def _apply_self_report(result: TriageResult, final_text: str) -> None:
    """Parse the model's own verdict and compare it against reality."""
    parsed = _extract_json(final_text)
    if not parsed:
        # No parseable verdict. Keep the raw text as reasoning so the
        # groundedness judge still has something to work with, and record that
        # the model never claimed a decision rather than pretending it agreed.
        result.reasoning = final_text.strip()
        result.self_reported_decision = None
        result.decision_consistent = result.decision == "NOISE" and not final_text
        return

    claimed = parsed.get("decision")
    claimed = str(claimed).strip().upper() if claimed is not None else None
    if claimed not in VALID_DECISIONS:
        claimed = None

    result.self_reported_decision = claimed
    result.reasoning = str(parsed.get("reasoning") or "").strip() or final_text.strip()
    result.evidence = _as_str_list(parsed.get("evidence"))

    runbook = parsed.get("runbook_cited")
    result.runbook_cited = (
        str(runbook).strip()
        if runbook not in (None, "", "null", "none", "None")
        else None
    )

    severity = parsed.get("severity")
    result.severity = (
        str(severity).strip().upper()
        if severity not in (None, "", "null", "none", "None")
        else None
    )

    result.decision_consistent = claimed == result.decision
    if not result.decision_consistent:
        log_warn(
            "decision_self_report_mismatch",
            correlation_id=result.correlation_id,
            claimed=claimed,
            actual=result.decision,
            tools_called=result.tool_names(),
        )
