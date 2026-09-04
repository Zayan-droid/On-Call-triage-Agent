"""The four evaluation metrics.

  1. Tool selection accuracy   -- deterministic
  2. Tool parameter accuracy   -- deterministic
  3. Groundedness              -- judged (see eval/judge.py)
  4. Escalation correctness    -- deterministic, reported as precision/recall

Two of the four need no model at all. That is a cost argument (a full 38-case
sweep scores selection, parameters and escalation for zero tokens), a latency
argument, and above all a *drift* argument: a deterministic scorer returns the
same number for the same trace forever, so a change in the score is always a
change in the agent. Only groundedness asks a genuinely subjective question, and
only groundedness pays for a judge.

Three decisions in here shape what the numbers mean:

* **Action tools are excluded from tool selection.** `expected_tools` contains
  only investigation tools. Whether the agent called `page_oncall` is metric 4's
  entire job; counting it in metric 1 as well would double-weight escalation and
  make two "independent" metrics move together.

* **Parameter accuracy is conditional on selection.** A tool that was never
  called contributes nothing to metric 2. Scoring absent calls as wrong
  parameters would report one mistake twice, and would make metric 2 mostly a
  noisy restatement of metric 1. Coverage is reported alongside so a high
  parameter score on two calls cannot be mistaken for a high score on ten.

* **Undefined ratios are `None`, never `0.0`.** Precision with no positive
  predictions is undefined. Reporting it as zero would make a run that correctly
  stayed silent on every case look like total failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from agent.tools import normalise_metric

INVESTIGATION_TOOLS = {"get_service_metrics", "get_recent_deploys", "search_runbook"}
ACTION_TOOLS = {"create_incident", "page_oncall"}

# Cost asymmetry between the two escalation errors. A missed page means an
# outage runs longer; a false page burns an engineer's night and, repeated,
# causes the alert fatigue that makes every future page less effective.
#
# 5:1 is a judgement call, not a measurement, and it is exposed as a parameter
# precisely so the number it produces can be argued with. What matters is that
# the weights are stated rather than hidden inside a single accuracy figure.
DEFAULT_MISS_WEIGHT = 5.0
DEFAULT_FALSE_PAGE_WEIGHT = 1.0


def _safe_div(numerator: float, denominator: float) -> float | None:
    return None if not denominator else numerator / denominator


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or (precision + recall) == 0:
        return None
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------
# Metric 1: tool selection
# --------------------------------------------------------------------------


def score_tool_selection(case: dict, tool_names: Iterable[str]) -> dict:
    """Did the agent gather the evidence the case requires?

    `exact_match` -- the headline -- is: every required investigation tool was
    called, and no *forbidden* tool was called.

    Note what is deliberately NOT an error: calling an investigation tool the
    case did not require. All three investigation tools are read-only and cost
    one DynamoDB read or one CloudWatch query. An agent that checks the deploy
    history on a case where the metrics alone settle it has not made a mistake;
    it has spent a few hundred tokens. Grading that as incorrect would push the
    agent towards investigating *less*, which is the exact failure this project
    exists to measure.

    Over-investigation is real, though, so it is reported -- as `extra_calls`,
    alongside token counts, where it belongs: a cost signal, not a correctness
    one. Under-investigation is a correctness failure and `exact_match` catches
    it. `forbidden_tools` stays available per case for a call that would be
    genuinely wrong rather than merely unnecessary.
    """
    required = set(case.get("expected_tools") or [])
    forbidden = set(case.get("forbidden_tools") or [])

    names = list(tool_names)
    called = {name for name in names if name in INVESTIGATION_TOOLS}

    matched = required & called
    missing = sorted(required - called)
    violated = sorted(forbidden & set(names))
    extra = sorted(called - required)

    recall = _safe_div(len(matched), len(required)) if required else 1.0
    precision = _safe_div(len(matched), len(called)) if called else (
        1.0 if not required else 0.0
    )

    return {
        "required": sorted(required),
        "called": sorted(called),
        "missing": missing,
        "forbidden_called": violated,
        "extra_calls": extra,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "exact_match": not missing and not violated,
        "investigated_at_all": bool(called),
        # Repeated calls to the same tool: retries after an error, or a loop.
        "total_investigation_calls": sum(
            1 for n in names if n in INVESTIGATION_TOOLS
        ),
    }


# --------------------------------------------------------------------------
# Metric 2: tool parameter accuracy
# --------------------------------------------------------------------------


def _compare(key: str, rule: dict, actual: Any) -> tuple[bool, str]:
    """Apply one comparator. Returns (passed, human-readable explanation)."""
    if actual is None:
        return False, f"{key} was not supplied"

    if "equals" in rule:
        want = rule["equals"]
        # Metric names are compared after the same alias normalisation the tool
        # itself applies. `cpu` and `CPUUtilization` produce an identical
        # CloudWatch call, so grading them differently would measure the model's
        # spelling rather than whether it queried the right thing.
        if key == "metric":
            ok = normalise_metric(str(actual)) == normalise_metric(str(want))
        else:
            ok = str(actual).strip() == str(want).strip()
        return ok, f"{key}={actual!r} vs expected {want!r}"

    if "one_of" in rule:
        options = [str(o).strip() for o in rule["one_of"]]
        return str(actual).strip() in options, f"{key}={actual!r} vs one of {options}"

    if "contains_any" in rule:
        haystack = str(actual).lower()
        needles = [str(n).lower() for n in rule["contains_any"]]
        hit = next((n for n in needles if n in haystack), None)
        return hit is not None, (
            f"{key}={actual!r} contains {hit!r}"
            if hit
            else f"{key}={actual!r} contains none of {needles}"
        )

    if "min" in rule or "max" in rule:
        try:
            number = float(actual)
        except (TypeError, ValueError):
            return False, f"{key}={actual!r} is not numeric"
        low = rule.get("min")
        high = rule.get("max")
        ok = (low is None or number >= low) and (high is None or number <= high)
        return ok, f"{key}={number} vs range [{low}, {high}]"

    raise ValueError(f"Unknown comparator for '{key}': {sorted(rule)}")


def _call_score(call: Any, params: dict) -> int:
    """How many of `params` this one call satisfies. Used to pick the best try."""
    return sum(
        1
        for key, rule in params.items()
        if _compare(key, rule, (call.arguments or {}).get(key))[0]
    )


def score_tool_parameters(case: dict, trace: list) -> dict:
    """Were the arguments right, for the tools that were actually called?

    When a tool was called more than once -- which happens legitimately after a
    validation error, or when the agent widens its window on a second look --
    the best attempt is scored. Grading the first attempt would punish an agent
    for recovering from an error, which is behaviour worth encouraging.
    """
    expected = case.get("expected_params") or {}
    checks: list[dict] = []
    tools_expected = 0
    tools_scored = 0

    for tool, params in expected.items():
        tools_expected += 1
        calls = [c for c in trace if c.name == tool]
        if not calls:
            continue  # conditional on selection -- metric 1 already counted this
        tools_scored += 1
        best = max(calls, key=lambda c: _call_score(c, params))
        for key, rule in params.items():
            ok, detail = _compare(key, rule, (best.arguments or {}).get(key))
            checks.append(
                {
                    "tool": tool,
                    "param": key,
                    "passed": ok,
                    "detail": detail,
                    "attempts": len(calls),
                }
            )

    passed = sum(1 for c in checks if c["passed"])
    return {
        "checks": checks,
        "checks_total": len(checks),
        "checks_passed": passed,
        # None, not 0.0: no call means no evidence about parameters either way.
        "accuracy": _safe_div(passed, len(checks)),
        "tools_expected": tools_expected,
        "tools_scored": tools_scored,
        "coverage": _safe_div(tools_scored, tools_expected) if tools_expected else 1.0,
        "failures": [c["detail"] for c in checks if not c["passed"]],
    }


# --------------------------------------------------------------------------
# Metric 4: escalation correctness
# --------------------------------------------------------------------------


def classify_escalation(expected_page: bool, actual_paged: bool) -> str:
    if expected_page and actual_paged:
        return "TP"
    if expected_page and not actual_paged:
        return "FN"  # missed page: an outage runs longer
    if not expected_page and actual_paged:
        return "FP"  # false page: someone's night, and alert fatigue
    return "TN"


def aggregate_escalation(
    labels: Iterable[str],
    *,
    miss_weight: float = DEFAULT_MISS_WEIGHT,
    false_page_weight: float = DEFAULT_FALSE_PAGE_WEIGHT,
) -> dict:
    labels = list(labels)
    tp = labels.count("TP")
    fp = labels.count("FP")
    tn = labels.count("TN")
    fn = labels.count("FN")
    total = len(labels)

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)

    return {
        "n": total,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "accuracy": _safe_div(tp + tn, total),
        # Of the alerts that should NOT have paged, what fraction did. This is
        # the number that predicts alert fatigue, and it is deliberately not
        # 1 - precision: precision moves with how many true pages there were,
        # false-page rate does not.
        "false_page_rate": _safe_div(fp, fp + tn),
        "missed_page_rate": _safe_div(fn, tp + fn),
        "weighted_cost": _safe_div(
            miss_weight * fn + false_page_weight * fp, total
        ),
        "weights": {"miss": miss_weight, "false_page": false_page_weight},
    }


# --------------------------------------------------------------------------
# Per-case assembly
# --------------------------------------------------------------------------


@dataclass
class CaseScore:
    case_id: str
    bucket: str
    expected_page: bool
    actual_paged: bool
    decision: str
    escalation: str
    tool_selection: dict
    tool_parameters: dict
    groundedness: dict | None = None
    decision_consistent: bool = True
    hit_iteration_cap: bool = False
    degraded: bool = False
    error: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tools_called: list[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "bucket": self.bucket,
            "expected_page": self.expected_page,
            "actual_paged": self.actual_paged,
            "decision": self.decision,
            "escalation": self.escalation,
            "tool_selection": self.tool_selection,
            "tool_parameters": self.tool_parameters,
            "groundedness": self.groundedness,
            "decision_consistent": self.decision_consistent,
            "hit_iteration_cap": self.hit_iteration_cap,
            "degraded": self.degraded,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tools_called": self.tools_called,
            "reasoning": self.reasoning,
        }


def score_case(case: dict, result: Any) -> CaseScore:
    """Score one case against one triage result (groundedness attached later)."""
    return CaseScore(
        case_id=case["id"],
        bucket=case.get("bucket", "unknown"),
        expected_page=bool(case["expected_page"]),
        actual_paged=bool(result.paged),
        decision=result.decision,
        escalation=classify_escalation(bool(case["expected_page"]), bool(result.paged)),
        tool_selection=score_tool_selection(case, result.tool_names()),
        tool_parameters=score_tool_parameters(case, result.trace),
        decision_consistent=result.decision_consistent,
        hit_iteration_cap=result.hit_iteration_cap,
        degraded=bool(result.error),
        error=result.error,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        tools_called=result.tool_names(),
        reasoning=result.reasoning,
    )


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def aggregate(
    scores: list[CaseScore],
    *,
    miss_weight: float = DEFAULT_MISS_WEIGHT,
    false_page_weight: float = DEFAULT_FALSE_PAGE_WEIGHT,
) -> dict:
    """Roll per-case scores into the run summary, overall and per bucket."""
    if not scores:
        return {"n": 0}

    def summarise(subset: list[CaseScore]) -> dict:
        grounded = [
            s.groundedness["score"]
            for s in subset
            if s.groundedness and s.groundedness.get("score") is not None
        ]
        return {
            "n": len(subset),
            "tool_selection_exact": _mean(
                [1.0 if s.tool_selection["exact_match"] else 0.0 for s in subset]
            ),
            "tool_selection_f1": _mean([s.tool_selection["f1"] for s in subset]),
            "tool_selection_recall": _mean([s.tool_selection["recall"] for s in subset]),
            "investigated_at_all": _mean(
                [1.0 if s.tool_selection["investigated_at_all"] else 0.0 for s in subset]
            ),
            "mean_investigation_calls": _mean(
                [float(s.tool_selection["total_investigation_calls"]) for s in subset]
            ),
            "tool_parameter_accuracy": _mean(
                [s.tool_parameters["accuracy"] for s in subset]
            ),
            "tool_parameter_coverage": _mean(
                [s.tool_parameters["coverage"] for s in subset]
            ),
            "groundedness": _mean(grounded),
            "groundedness_scored": len(grounded),
            "escalation": aggregate_escalation(
                [s.escalation for s in subset],
                miss_weight=miss_weight,
                false_page_weight=false_page_weight,
            ),
            "decision_consistency": _mean(
                [1.0 if s.decision_consistent else 0.0 for s in subset]
            ),
            "iteration_cap_hits": sum(1 for s in subset if s.hit_iteration_cap),
            "degraded_cases": sum(1 for s in subset if s.degraded),
            "mean_latency_ms": _mean([s.latency_ms for s in subset]),
            "total_input_tokens": sum(s.input_tokens for s in subset),
            "total_output_tokens": sum(s.output_tokens for s in subset),
        }

    buckets = sorted({s.bucket for s in scores})
    return {
        "overall": summarise(scores),
        "by_bucket": {b: summarise([s for s in scores if s.bucket == b]) for b in buckets},
        "failures": [
            {
                "case_id": s.case_id,
                "bucket": s.bucket,
                "escalation": s.escalation,
                "expected_page": s.expected_page,
                "actual_paged": s.actual_paged,
                "missing_tools": s.tool_selection["missing"],
                "forbidden_called": s.tool_selection["forbidden_called"],
                "param_failures": s.tool_parameters["failures"],
                "groundedness": (s.groundedness or {}).get("score"),
                "error": s.error,
            }
            for s in scores
            if s.escalation in ("FP", "FN")
            or not s.tool_selection["exact_match"]
            or (s.tool_parameters["accuracy"] or 1.0) < 1.0
            or s.error
        ],
    }
