"""A scripted stand-in for Bedrock Converse.

Bedrock is the one dependency with no local equivalent, so this plays the model.
It speaks the real Converse wire format -- `toolUse` blocks, `stopReason`,
`usage` -- and reconstructs its own state by reading the `messages` list it is
handed, exactly as the real service does. That means the loop in
`agent/agent.py` is exercised for real: parallel tool blocks, `toolResult`
plumbing, the iteration cap, the forced-decision turn.

**What this is for, and what it is not.** It is a fixture. It makes the harness,
the tools, the scorers and the dedupe testable without a network, without
credentials, and without spending anything. It is emphatically *not* a claim
about how a real model performs -- a sweep against `POLICY_GOOD` measures the
harness, and the report labels it as such. Real numbers come from
`--mode aws --model <id>`.

The deliberately-broken policies matter as much as the good one. A harness that
has only ever seen correct behaviour has never demonstrated that it can detect
incorrect behaviour, and the noise bucket exists precisely to catch a
trigger-happy agent. `POLICY_TRIGGER_HAPPY` is that agent, on demand.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

POLICY_GOOD = "good"
POLICY_TRIGGER_HAPPY = "trigger_happy"
POLICY_LAZY = "lazy"
POLICY_HALLUCINATOR = "hallucinator"
POLICY_BAD_PARAMS = "bad_params"
POLICY_NEVER_STOPS = "never_stops"
POLICY_NO_JSON = "no_json"
POLICY_PHANTOM_INCIDENT = "phantom_incident"
POLICY_PROMPT_SENSITIVE = "prompt_sensitive"

POLICIES = (
    POLICY_GOOD,
    POLICY_TRIGGER_HAPPY,
    POLICY_LAZY,
    POLICY_HALLUCINATOR,
    POLICY_BAD_PARAMS,
    POLICY_NEVER_STOPS,
    POLICY_NO_JSON,
    POLICY_PHANTOM_INCIDENT,
    POLICY_PROMPT_SENSITIVE,
)

# The marker `prompt_sensitive` looks for to tell the two variants apart. It is
# a literal string from `agent/prompts.py`; if that heading is reworded, the
# policy stops distinguishing the variants and the offline experiment reports a
# null result -- so `tests/test_prompts.py` asserts the marker still exists.
V2_MARKER = "STAYING QUIET IS A VALID AND OFTEN CORRECT OUTCOME"


class ThrottlingException(Exception):
    """Shaped like a botocore ClientError so the retry path treats it as one."""

    def __init__(self, message: str = "Rate exceeded"):
        super().__init__(message)
        self.response = {"Error": {"Code": "ThrottlingException", "Message": message}}


# --------------------------------------------------------------------------
# Reading conversation state back out of `messages`
# --------------------------------------------------------------------------


def _history(messages: list[dict]) -> list[tuple[str, dict, dict | None]]:
    """Reconstruct [(tool_name, arguments, result_or_None)] in call order."""
    requested: list[tuple[str, str, dict]] = []  # (toolUseId, name, input)
    results: dict[str, dict] = {}

    for message in messages:
        for block in message.get("content", []):
            use = block.get("toolUse")
            if use:
                requested.append(
                    (use.get("toolUseId", ""), use.get("name", ""), use.get("input") or {})
                )
            outcome = block.get("toolResult")
            if outcome:
                payload: dict = {}
                for part in outcome.get("content", []):
                    if "json" in part:
                        payload = part["json"]
                results[outcome.get("toolUseId", "")] = payload

    return [(name, args, results.get(use_id)) for use_id, name, args in requested]


def _last_result(history: list, tool: str) -> dict | None:
    for name, _args, result in reversed(history):
        if name == tool and result is not None:
            return result
    return None


def _metric_results(history: list) -> list[dict]:
    return [
        result
        for name, _args, result in history
        if name == "get_service_metrics" and isinstance(result, dict)
    ]


# --------------------------------------------------------------------------
# The reference decision logic
# --------------------------------------------------------------------------


def _breaching(value: float | None, threshold: float | None, comparison: str) -> bool:
    """Is this value on the wrong side of the threshold?

    Comparison-aware because not every alarm fires on 'greater than' -- a
    FreeableMemory alarm fires when the value is *below* its threshold, and
    treating that as 'not breaching' would make every memory case a missed page.
    """
    if value is None or threshold is None:
        return False
    if "Less" in comparison:
        return value <= threshold if "OrEqual" in comparison else value < threshold
    return value >= threshold if "OrEqual" in comparison else value > threshold


def _window_for(alert: dict) -> int:
    """Three times the alert duration, floored at 30 minutes, capped at 180.

    The system prompt asks for 2-3x the alert duration so the agent can see what
    came before the breach. The floor stops a one-minute alarm producing a
    useless three-minute window.
    """
    duration = alert.get("duration_min") or 10
    return max(30, min(180, int(duration * 3)))


def _decide(alert: dict, history: list) -> tuple[str, str, list[str]]:
    """Return (decision, severity, evidence) from tool evidence alone.

    This function never sees `expected_page`. If it did, the harness would be
    grading the dataset against itself and every score would be 1.0 by
    construction.
    """
    environment = str(alert.get("environment", "prod")).lower()
    threshold = alert.get("threshold")
    comparison = str(alert.get("comparison", "GreaterThanThreshold"))
    evidence: list[str] = []

    primary = None
    for result in _metric_results(history):
        if result.get("metric") == alert.get("metric") or primary is None:
            primary = result

    if environment != "prod":
        evidence.append(
            f"The alarm is on the {environment} environment, which has no users on it."
        )
        return "INCIDENT_ONLY", "SEV4", evidence

    if not primary or primary.get("summary") is None:
        evidence.append(
            "CloudWatch returned no datapoints, so the alarm could not be confirmed."
        )
        return "INCIDENT_ONLY", "SEV3", evidence

    summary = primary["summary"]
    latest = summary.get("latest")
    peak = summary.get("max")
    trough = summary.get("min")
    trend = summary.get("trend")
    evidence.append(
        f"{primary['metric']} on {primary['service']} over the last "
        f"{primary['window_minutes']} minutes: latest {latest}, max {peak}, "
        f"min {trough}, trend {trend}."
    )

    if not _breaching(latest, threshold, comparison):
        if _breaching(peak, threshold, comparison):
            evidence.append(
                f"The metric did breach (peak {peak} against threshold {threshold}) "
                f"but the most recent value {latest} is back within bounds."
            )
        else:
            evidence.append(
                f"No datapoint in the window breached the threshold of {threshold}; "
                f"the alarm and the metric history disagree."
            )
        return "INCIDENT_ONLY", "SEV4", evidence

    # Volume alone is not an incident. If the alarm is on request count and the
    # error rate was checked and is healthy, the service is absorbing the load.
    if str(alert.get("metric", "")).lower() == "requestcount":
        for result in _metric_results(history):
            errors = (result.get("summary") or {}) if result.get("metric") == "Error5xxRate" else {}
            if errors:
                evidence.append(
                    f"Error5xxRate is {errors.get('latest')} with a max of "
                    f"{errors.get('max')} across the same window, so the service "
                    f"is serving the extra volume successfully."
                )
                return "INCIDENT_ONLY", "SEV4", evidence

    deploys = (_last_result(history, "get_recent_deploys") or {}).get("deploys") or []
    if deploys:
        newest = deploys[0]
        evidence.append(
            f"Deploy {newest.get('version')} landed {newest.get('minutes_ago')} "
            f"minutes ago: {newest.get('change_summary')}"
        )
    else:
        evidence.append("No deploy is recorded for this service in the lookback window.")

    severity = _severity_for(alert)
    evidence.append(
        f"The breach is current ({latest} against a threshold of {threshold}) "
        f"and the trend is {trend}."
    )
    return "PAGE", severity, evidence


def _severity_for(alert: dict) -> str:
    return (
        "SEV1"
        if str(alert.get("service", "")).split("-")[0] in {"payments", "auth", "checkout"}
        else "SEV2"
    )


def _decide_naive(alert: dict, history: list) -> tuple[str, str, list[str]]:
    """The baseline agent: checks the metric is still breaching, and stops there.

    This is what the `v1_baseline` prompt gets, and it is deliberately not a
    strawman. It does investigate -- it pulls the metric history and correctly
    declines to page on anything that has already recovered, spiked once, or is
    flapping. What it lacks is everything the `v2_investigate_first` prompt adds:
    that a non-production environment has no users on it, that volume without
    errors is not an incident, and that the runbook may say not to page.

    Those are precisely the cases where a competent-looking agent still burns
    somebody's night, which is what makes the delta between the two variants
    worth measuring rather than obvious.
    """
    threshold = alert.get("threshold")
    comparison = str(alert.get("comparison", "GreaterThanThreshold"))
    evidence: list[str] = []

    primary = None
    for result in _metric_results(history):
        if result.get("metric") == alert.get("metric") or primary is None:
            primary = result

    if not primary or primary.get("summary") is None:
        evidence.append("CloudWatch returned no datapoints for this metric.")
        return "INCIDENT_ONLY", "SEV3", evidence

    summary = primary["summary"]
    latest = summary.get("latest")
    evidence.append(
        f"{primary['metric']} on {primary['service']}: latest {latest}, "
        f"max {summary.get('max')}, trend {summary.get('trend')}."
    )

    if not _breaching(latest, threshold, comparison):
        evidence.append(
            f"The latest value {latest} is within the threshold of {threshold}."
        )
        return "INCIDENT_ONLY", "SEV4", evidence

    deploys = (_last_result(history, "get_recent_deploys") or {}).get("deploys") or []
    if deploys:
        evidence.append(
            f"Deploy {deploys[0].get('version')} landed "
            f"{deploys[0].get('minutes_ago')} minutes ago."
        )
    evidence.append(
        f"The threshold of {threshold} is still being breached at {latest}."
    )
    return "PAGE", _severity_for(alert), evidence


# --------------------------------------------------------------------------
# The fake client
# --------------------------------------------------------------------------


class ScriptedBedrock:
    """Implements the one method `agent.agent` calls: `converse`."""

    def __init__(
        self,
        policy: str = POLICY_GOOD,
        *,
        throttle_times: int = 0,
        fail_with: Exception | None = None,
        hook: Callable[[dict], None] | None = None,
    ):
        if policy not in POLICIES:
            raise ValueError(f"Unknown policy '{policy}'. Known: {', '.join(POLICIES)}")
        self.policy = policy
        self.throttle_times = throttle_times
        self.fail_with = fail_with
        self.hook = hook
        self.calls = 0
        self._ids = itertools.count(1)

    # -- response builders --------------------------------------------------

    def _tool_use(self, name: str, arguments: dict) -> dict:
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": f"tu_{next(self._ids)}",
                                "name": name,
                                "input": arguments,
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 900, "outputTokens": 60},
        }

    def _text(self, text: str) -> dict:
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1200, "outputTokens": 180},
        }

    def _verdict(
        self,
        decision: str,
        severity: str | None,
        reasoning: str,
        evidence: list[str],
        runbook: str | None,
    ) -> dict:
        return self._text(
            json.dumps(
                {
                    "decision": decision,
                    "severity": severity,
                    "reasoning": reasoning,
                    "evidence": evidence,
                    "runbook_cited": runbook,
                },
                indent=2,
            )
        )

    # -- the entrypoint -----------------------------------------------------

    def converse(self, **kwargs: Any) -> dict:
        self.calls += 1
        if self.hook:
            self.hook(kwargs)
        if self.throttle_times > 0:
            self.throttle_times -= 1
            raise ThrottlingException
        if self.fail_with is not None:
            raise self.fail_with

        messages = kwargs.get("messages") or []
        alert = _parse_alert(messages)
        history = _history(messages)
        tools_used = [name for name, _a, _r in history]
        # No toolConfig means the loop dropped it for the forced-decision turn.
        can_use_tools = bool(kwargs.get("toolConfig"))
        system_text = "\n".join(
            block.get("text", "") for block in (kwargs.get("system") or [])
        )

        return self._step(alert, history, tools_used, can_use_tools, system_text)

    # -- policies -----------------------------------------------------------

    def _step(
        self,
        alert: dict,
        history: list,
        used: list[str],
        can_use_tools: bool,
        system_text: str = "",
    ) -> dict:
        policy = self.policy

        # `prompt_sensitive` is the only policy that reads the system prompt. It
        # exists so the A/B experiment pipeline -- two runs, a comparison table,
        # a generated headline sentence -- can be exercised and tested offline.
        # It simulates the effect of the prompt change; it does not measure one.
        # A real number requires --mode aws and a real model, and the report
        # labels every offline run accordingly.
        naive = policy == POLICY_PROMPT_SENSITIVE and V2_MARKER not in system_text
        if policy == POLICY_PROMPT_SENSITIVE:
            policy = POLICY_GOOD

        if policy == POLICY_LAZY:
            return self._verdict(
                "NOISE",
                None,
                "This looks like routine variation and does not need attention.",
                [],
                None,
            )

        if policy == POLICY_NO_JSON:
            return self._text(
                "I looked at the alert and I think somebody should probably take a "
                "look at it in the morning. Hard to say for certain."
            )

        if policy == POLICY_NEVER_STOPS:
            # Always asks for another tool. Exercises the iteration cap and the
            # forced-decision turn; without tools available it must answer.
            if not can_use_tools:
                return self._verdict(
                    "NOISE", "SEV4", "Ran out of investigation budget.", [], None
                )
            return self._tool_use(
                "get_service_metrics",
                {
                    "service": alert.get("service"),
                    "metric": alert.get("metric"),
                    "window_minutes": _window_for(alert),
                },
            )

        if policy == POLICY_PHANTOM_INCIDENT:
            # Pages against an incident id that was never created. Exercises the
            # page_oncall guardrail and its recovery path.
            if "page_oncall" not in used:
                return self._tool_use(
                    "page_oncall",
                    {"incident_id": "INC-20260101-DEADBEEF", "reason": "Looks bad."},
                )
            return self._verdict(
                "PAGE", "SEV1", "I attempted to page the on-call engineer.", [], None
            )

        if policy == POLICY_TRIGGER_HAPPY:
            # The failure mode the noise bucket exists to catch: escalate first,
            # investigate never.
            if "create_incident" not in used:
                return self._tool_use(
                    "create_incident",
                    {
                        "severity": "SEV1",
                        "summary": f"{alert.get('service')} {alert.get('metric')} "
                        f"is {alert.get('value')} against a threshold of "
                        f"{alert.get('threshold')}.",
                    },
                )
            if "page_oncall" not in used:
                incident = _last_result(history, "create_incident") or {}
                return self._tool_use(
                    "page_oncall",
                    {
                        "incident_id": incident.get("incident_id", "unknown"),
                        "reason": "The alarm threshold was crossed.",
                    },
                )
            return self._verdict(
                "PAGE",
                "SEV1",
                f"{alert.get('service')} breached its threshold, so I paged the on-call.",
                [f"Alarm value {alert.get('value')} against threshold {alert.get('threshold')}."],
                None,
            )

        # --- POLICY_GOOD, POLICY_HALLUCINATOR, POLICY_BAD_PARAMS ------------
        wrong = policy == POLICY_BAD_PARAMS

        if "get_service_metrics" not in used:
            return self._tool_use(
                "get_service_metrics",
                {
                    # A plausible mistake, not a random one: the service name
                    # with its environment suffix stripped, and a window far too
                    # short to tell sustained from transient.
                    "service": (
                        str(alert.get("service", "")).rsplit("-", 1)[0]
                        if wrong
                        else alert.get("service")
                    ),
                    "metric": alert.get("metric"),
                    "window_minutes": 5 if wrong else _window_for(alert),
                },
            )

        # Volume alarms need the error rate before they can be interpreted.
        if (
            not naive
            and str(alert.get("metric", "")).lower() == "requestcount"
            and not any(
                (r or {}).get("metric") == "Error5xxRate" for _n, _a, r in history
            )
        ):
            return self._tool_use(
                "get_service_metrics",
                {
                    "service": alert.get("service"),
                    "metric": "Error5xxRate",
                    "window_minutes": _window_for(alert),
                },
            )

        if "get_recent_deploys" not in used:
            return self._tool_use(
                "get_recent_deploys",
                {"service": alert.get("service"), "hours": 24},
            )

        # The baseline never consults the runbook: nothing in the v1 prompt tells
        # it to, so it misses the two cases whose correct answer is written there.
        if not naive and "search_runbook" not in used:
            return self._tool_use(
                "search_runbook",
                {"symptom": _symptom(alert)},
            )

        decision, severity, evidence = (
            _decide_naive(alert, history) if naive else _decide(alert, history)
        )
        runbook = None
        matches = (_last_result(history, "search_runbook") or {}).get("matches") or []
        if matches:
            runbook = matches[0].get("runbook_id")

        if decision in ("PAGE", "INCIDENT_ONLY") and "create_incident" not in used:
            return self._tool_use(
                "create_incident",
                {"severity": severity or "SEV3", "summary": " ".join(evidence)[:900]},
            )

        if decision == "PAGE" and "page_oncall" not in used:
            incident = _last_result(history, "create_incident") or {}
            return self._tool_use(
                "page_oncall",
                {
                    "incident_id": incident.get("incident_id", "unknown"),
                    "reason": " ".join(evidence)[:900],
                },
            )

        reasoning = " ".join(evidence)
        if policy == POLICY_HALLUCINATOR:
            # Numbers no tool returned, and a runbook id that does not exist.
            reasoning = (
                "CPU has been pinned at 99.7% for 41 minutes and the error rate "
                "reached 17.3%, which the runbook says is a hard page. The deploy "
                "1400 minutes ago is the likely cause."
            )
            runbook = "RB-999"

        return self._verdict(decision, severity, reasoning, evidence, runbook)


# --------------------------------------------------------------------------
# Parsing the alert back out of the rendered user turn
# --------------------------------------------------------------------------

_NUMERIC_FIELDS = {"value", "threshold", "duration_min"}


def _parse_alert(messages: list[dict]) -> dict:
    """Read the alert back out of the first user message.

    The fake deliberately parses the rendered prompt rather than being handed
    the alert object. If it were handed the object it would be testing a
    different input than the one the real model sees, and a prompt-rendering bug
    would sail straight through every offline test.
    """
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            text = block.get("text")
            if not text or "ALERT" not in text:
                continue
            alert: dict = {}
            for line in text.splitlines():
                if ":" not in line or not line.startswith("  "):
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key in _NUMERIC_FIELDS:
                    try:
                        number = float(value)
                        alert[key] = int(number) if number.is_integer() else number
                    except ValueError:
                        alert[key] = None
                else:
                    alert[key] = value
            return alert
    return {}


def _symptom(alert: dict) -> str:
    """A natural-language symptom string, the way an operator would phrase it."""
    metric = str(alert.get("metric", "")).lower()
    phrase = {
        "cpuutilization": "sustained high cpu utilisation",
        "latencyp99": "elevated p99 latency",
        "error5xxrate": "elevated 5xx error rate",
        "queuedepth": "worker queue backlog growing",
        "processinglagseconds": "worker processing lag growing",
        "databaseconnections": "database connection pool near exhaustion",
        "freeablememory": "database freeable memory low",
        "throttles": "lambda throttling at the concurrency limit",
        "errors": "function errors above threshold",
        "duration": "function duration approaching timeout",
        "requestcount": "traffic spike in request volume",
    }.get(metric, f"{metric} above threshold")

    environment = str(alert.get("environment", "prod")).lower()
    if environment != "prod":
        phrase = f"{phrase} on a {environment} non-production environment"
    return f"{phrase} on {alert.get('service')}"
