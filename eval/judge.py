"""Groundedness: is the agent's reasoning supported by what the tools returned?

Two judges, run together by default:

**HeuristicJudge** -- deterministic, free, instant. It answers one narrow
question extremely well: *did the agent state a number no tool gave it?* Every
numeric literal in the reasoning is checked against the numbers that actually
appear in the tool results and the alert. It also checks that any cited runbook
id was really returned by `search_runbook`, and that the agent did not attribute
a claim to a tool it never called.

**BedrockJudge** -- an LLM scoring the same reasoning against the same trace with
a rubric. It catches what the heuristic cannot: a causal claim that no evidence
supports, a paraphrase of a runbook step that was never returned, a confident
narrative built on one datapoint.

Running both is the point, and it is cheap to do. They disagree in informative
ways, and the agreement rate between them is reported as its own number -- it is
the closest thing available to a validity check on the LLM judge. A judge nobody
has checked is just a second opinion with better formatting.

Neither judge sees the expected answer or the reference reasoning. A judge told
what the right decision was will rationalise towards it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Numeric-looking tokens that are identifiers, not quantities. Counting the "99"
# in "p99" or the "4" in "v4.11.0" as an unsupported figure would make the score
# a measure of the agent's vocabulary rather than its honesty.
_NON_QUANTITY = re.compile(
    r"""
      \b[45]xx\b                 # 5xx, 4xx
    | \bp\d{2,3}\b               # p50, p95, p99
    | \bSEV\d\b                  # SEV1..SEV4
    | \bv\d+(?:\.\d+)*\b         # v4.11.0
    | \bINC-[0-9A-Z-]+\b         # incident ids
    | \bRB-\d+\b                 # runbook ids
    | \b\d{4}-\d{2}-\d{2}T[\d:]+Z?\b   # ISO timestamps
    | \bHTTP\s*\d{3}\b           # HTTP 500
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# Relative and absolute slack when matching a stated number to an observed one.
# "CPU is at 96%" against an observed 95.7 is a correct reading of the graph, not
# a fabrication; demanding exactness would flag good reasoning as hallucinated.
REL_TOLERANCE = 0.02
ABS_TOLERANCE = 0.5


@dataclass
class GroundednessVerdict:
    score: float | None
    verdict: str
    justification: str
    unsupported: list[str] = field(default_factory=list)
    judged_by: str = "heuristic"
    numbers_checked: int = 0
    numbers_grounded: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "justification": self.justification,
            "unsupported": self.unsupported,
            "judged_by": self.judged_by,
            "numbers_checked": self.numbers_checked,
            "numbers_grounded": self.numbers_grounded,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Shared extraction
# --------------------------------------------------------------------------


def extract_numbers(text: str) -> list[float]:
    """Numeric quantities stated in prose, with identifiers stripped out."""
    if not text:
        return []
    cleaned = _NON_QUANTITY.sub(" ", text)
    out = []
    for token in _NUMBER.findall(cleaned):
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def _walk_numbers(node: Any, into: set[float]) -> None:
    if isinstance(node, bool):
        return  # bool is an int subclass; True would ground the number 1
    if isinstance(node, (int, float)):
        into.add(float(node))
    elif isinstance(node, str):
        into.update(extract_numbers(node))
    elif isinstance(node, dict):
        for value in node.values():
            _walk_numbers(value, into)
    elif isinstance(node, (list, tuple, set)):
        for value in node:
            _walk_numbers(value, into)


def salient_numbers(result: Any) -> set[float]:
    """The handful of figures an operator would actually reason *with*.

    The alert's value, threshold and duration, plus each metric summary's
    avg/max/min/latest/first. Roughly a dozen numbers, not the hundreds in the
    raw datapoint arrays -- which is what makes deriving from them safe.
    """
    salient: set[float] = set()
    for key in ("value", "threshold", "duration_min"):
        _walk_numbers((result.alert or {}).get(key), salient)
    for call in result.trace:
        if not call.ok or not isinstance(call.result, dict):
            continue
        _walk_numbers(call.result.get("summary"), salient)
    return {n for n in salient if n is not None}


def derived_numbers(salient: set[float]) -> set[float]:
    """Arithmetic an operator does out loud, from the salient figures only.

    "CPU is 14 points over the 80 threshold" is a correct subtraction, not a
    fabrication, and flagging it as one would make the judge penalise exactly
    the reasoning it is meant to reward. So pairwise differences and ratios of
    the salient figures count as grounded.

    Three deliberate restrictions, each of which was needed to stop the judge
    losing its teeth:

    * **Salient figures only, not the full observed set.** A metric window holds
      hundreds of datapoints; all their pairwise differences would be tens of
      thousands of values, and with any tolerance at all almost every number in
      range would come back grounded. A judge that agrees with everything is
      worse than one that is occasionally too strict.
    * **No percentage changes.** They were tried and removed. `(80-40.1)/40.1`
      is 99.5, which grounded an invented "pinned at 99.7%" -- percentage
      derivations spread across 0-200 densely enough to blanket the range where
      fabricated percentages live. The cost is that "rose 17.5% above the
      threshold" now reads as ungrounded; stating the two figures instead is
      both clearer and directly checkable.
    * **Ratios only in the larger-over-smaller direction.** Sub-1 ratios cluster
      tightly, and with a 0.5 absolute tolerance any of them would ground any
      small number.
    """
    values = sorted(salient)
    derived: set[float] = set()
    for i, smaller in enumerate(values):
        for larger in values[i + 1 :]:
            derived.add(larger - smaller)
            if smaller:
                derived.add(larger / smaller)
    return derived


def collect_observed_numbers(result: Any) -> set[float]:
    """Every number the agent could legitimately state.

    Three sources: what the tools returned, what was in the alert, and simple
    arithmetic over the salient figures (see `derived_numbers`).
    """
    observed: set[float] = set()
    for call in result.trace:
        _walk_numbers(call.result, observed)
        _walk_numbers(call.arguments, observed)
    _walk_numbers(result.alert, observed)
    return observed | derived_numbers(salient_numbers(result))


def _is_grounded(value: float, observed: set[float]) -> bool:
    for candidate in observed:
        tolerance = max(abs(candidate) * REL_TOLERANCE, ABS_TOLERANCE)
        if abs(value - candidate) <= tolerance:
            return True
    return False


def cited_runbook_ids(result: Any) -> set[str]:
    ids: set[str] = set()
    for call in result.trace:
        if call.name != "search_runbook" or not call.ok:
            continue
        for match in (call.result or {}).get("matches", []) or []:
            if match.get("runbook_id"):
                ids.add(str(match["runbook_id"]))
    return ids


# --------------------------------------------------------------------------
# Heuristic judge
# --------------------------------------------------------------------------

# Phrases that assert a tool was consulted. Used to catch reasoning that
# attributes evidence to a tool the agent never called.
_TOOL_CLAIMS = {
    "get_recent_deploys": (
        r"\brecent deploy|\bdeployed\b|\brelease\b|\brollback\b|\brolled back\b|\bdeploy(?:ment)?\b"
    ),
    "search_runbook": r"\brunbook\b|\bremediation\b|\bplaybook\b",
    "get_service_metrics": r"\bcloudwatch\b|\bdatapoint|\bmetric history\b|\btrend\b",
}


class HeuristicJudge:
    """Deterministic groundedness. No model, no cost, no drift."""

    name = "heuristic"

    def judge(self, case: dict, result: Any) -> GroundednessVerdict:
        reasoning = (result.reasoning or "").strip()
        if not reasoning:
            return GroundednessVerdict(
                score=None,
                verdict="no_reasoning",
                justification="The agent produced no reasoning text to check.",
                judged_by=self.name,
            )

        observed = collect_observed_numbers(result)
        stated = extract_numbers(reasoning)
        ungrounded = [v for v in stated if not _is_grounded(v, observed)]

        unsupported: list[str] = [
            f"stated the figure {v:g}, which appears in no tool result or in the alert"
            for v in dict.fromkeys(ungrounded)
        ]

        # A cited runbook id must have been returned by search_runbook.
        available = cited_runbook_ids(result)
        if result.runbook_cited and result.runbook_cited not in available:
            unsupported.append(
                f"cited runbook {result.runbook_cited!r}, which search_runbook did not return "
                f"(returned: {sorted(available) or 'nothing'})"
            )

        # Attributing evidence to a tool that was never called.
        called = {c.name for c in result.trace if c.ok}
        for tool, pattern in _TOOL_CLAIMS.items():
            if tool in called:
                continue
            if re.search(pattern, reasoning, re.IGNORECASE):
                unsupported.append(
                    f"reasoning refers to evidence from {tool}, which was never called successfully"
                )

        numeric_score = (
            (len(stated) - len(ungrounded)) / len(stated) if stated else 1.0
        )
        # Non-numeric violations are categorical, not fractional: inventing a
        # runbook id is not 5% wrong. Each costs a flat 0.25.
        penalty = 0.25 * (len(unsupported) - len(dict.fromkeys(ungrounded)))
        score = max(0.0, min(1.0, numeric_score - penalty))

        if not stated and not unsupported:
            verdict = "no_figures_cited"
            justification = (
                "The reasoning states no numbers and makes no unsupported "
                "attribution. Nothing was fabricated, but nothing was cited "
                "either -- treated as grounded, weakly."
            )
        elif not unsupported:
            verdict = "grounded"
            justification = (
                f"All {len(stated)} figures in the reasoning match values returned "
                f"by tools or present in the alert, within tolerance."
            )
        else:
            verdict = "ungrounded"
            justification = "; ".join(unsupported)

        return GroundednessVerdict(
            score=round(score, 4),
            verdict=verdict,
            justification=justification,
            unsupported=unsupported,
            judged_by=self.name,
            numbers_checked=len(stated),
            numbers_grounded=len(stated) - len(ungrounded),
        )


# --------------------------------------------------------------------------
# Bedrock judge
# --------------------------------------------------------------------------

JUDGE_SYSTEM = """\
You are grading a single claim of evidence, not an engineering decision.

You will be shown (a) the tool calls an automated triage agent made and exactly
what each tool returned, and (b) the reasoning the agent then wrote. Decide only
this: is every factual claim in the reasoning supported by the tool results or
by the alert itself?

Grade the grounding, not the judgement. If the agent reasoned correctly from the
evidence but reached a decision you disagree with, that is still grounded. If it
reached a decision you agree with using a number nothing returned, that is
ungrounded.

Count as UNSUPPORTED:
  - any figure that no tool returned and that was not in the alert;
  - a causal claim ("caused by the deploy") when nothing links the two;
  - remediation steps not present in a returned runbook;
  - a claim about a tool's output when that tool was never called;
  - describing a metric as rising, falling, or recovered when the datapoints do
    not show that.

Do NOT count as unsupported:
  - correct arithmetic on returned values (a difference, a ratio, a percentage);
  - sensible rounding of a returned figure;
  - stated uncertainty, or explicitly noting that a tool returned nothing;
  - operational judgement about severity or user impact.

Reply with a single JSON object and nothing else:
{"score": <0.0-1.0>, "verdict": "grounded" | "partially_grounded" | "ungrounded",
 "justification": "<two sentences naming what you checked>",
 "unsupported_claims": ["<quote the exact claim>", ...]}

score 1.0 = every claim supported. 0.0 = the central claim is fabricated.
"""


def compact_trace(result: Any, max_datapoints: int = 6) -> list[dict]:
    """Shrink the trace for the judge prompt.

    Full metric series are hundreds of datapoints; sending them would cost more
    than the triage itself and bury the summary the reasoning actually cites.
    The summary block is kept intact and the raw series is sampled from both
    ends, so a claim about a trend remains checkable.
    """
    compact = []
    for call in result.trace:
        entry: dict[str, Any] = {
            "tool": call.name,
            "arguments": call.arguments,
            "ok": call.ok,
        }
        if not call.ok:
            entry["error"] = call.error
            compact.append(entry)
            continue

        payload = dict(call.result or {})
        points = payload.get("datapoints")
        if isinstance(points, list) and len(points) > max_datapoints:
            head = max_datapoints // 2
            payload["datapoints"] = (
                points[:head]
                + [{"note": f"...{len(points) - max_datapoints} datapoints omitted..."}]
                + points[-(max_datapoints - head) :]
            )
        entry["result"] = payload
        compact.append(entry)
    return compact


class BedrockJudge:
    """LLM groundedness judge. Logs its justification, not just its score."""

    name = "bedrock"

    def __init__(self, client: Any, model_id: str, *, max_tokens: int = 800):
        self.client = client
        self.model_id = model_id
        self.max_tokens = max_tokens

    def _prompt(self, case: dict, result: Any) -> str:
        return (
            "ALERT\n"
            + json.dumps(result.alert, indent=2, default=str)
            + "\n\nTOOL CALLS AND RESULTS\n"
            + json.dumps(compact_trace(result), indent=2, default=str)
            + "\n\nAGENT REASONING TO GRADE\n"
            + (result.reasoning or "(the agent produced no reasoning)")
        )

    def judge(self, case: dict, result: Any) -> GroundednessVerdict:
        if not (result.reasoning or "").strip():
            return GroundednessVerdict(
                score=None,
                verdict="no_reasoning",
                justification="The agent produced no reasoning text to check.",
                judged_by=self.name,
            )
        try:
            response = self.client.converse(
                modelId=self.model_id,
                system=[{"text": JUDGE_SYSTEM}],
                messages=[{"role": "user", "content": [{"text": self._prompt(case, result)}]}],
                # Temperature 0: a judge that returns a different number for the
                # same trace on Tuesday is not a measurement.
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0.0},
            )
            text = "\n".join(
                block["text"]
                for block in response.get("output", {}).get("message", {}).get("content", [])
                if "text" in block
            )
        except Exception as exc:
            return GroundednessVerdict(
                score=None,
                verdict="judge_error",
                justification=f"The judge model call failed: {type(exc).__name__}: {exc}",
                judged_by=self.name,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )

        from agent.agent import _extract_json

        parsed = _extract_json(text)
        if not parsed:
            # A judge whose output could not be parsed scores nothing. Coercing
            # it to 0.0 would report the harness's failure as the agent's.
            return GroundednessVerdict(
                score=None,
                verdict="judge_unparseable",
                justification=f"Judge returned unparseable output: {text[:300]}",
                judged_by=self.name,
                error="unparseable_judge_output",
            )

        try:
            score = float(parsed.get("score"))
            score = max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            score = None

        return GroundednessVerdict(
            score=score,
            verdict=str(parsed.get("verdict") or "unknown"),
            justification=str(parsed.get("justification") or "")[:1500],
            unsupported=[str(c) for c in (parsed.get("unsupported_claims") or [])],
            judged_by=self.name,
        )


# --------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------

AGREEMENT_BAND = 0.25


class CompositeJudge:
    """Run both judges, report both, and report whether they agree.

    The headline groundedness score is the LLM judge's when it is available,
    because it answers the fuller question. The heuristic score travels beside it
    as a floor that cannot drift, and `agreement` says whether the two are
    telling the same story. A run where agreement collapses is a run where the
    judge needs looking at before its score is believed.
    """

    name = "composite"

    def __init__(self, heuristic: HeuristicJudge, model_judge: BedrockJudge | None):
        self.heuristic = heuristic
        self.model_judge = model_judge

    def judge(self, case: dict, result: Any) -> dict:
        heuristic = self.heuristic.judge(case, result).to_dict()
        if self.model_judge is None:
            return {
                **heuristic,
                "score": heuristic["score"],
                "heuristic": heuristic,
                "model": None,
                "agreement": None,
            }

        model = self.model_judge.judge(case, result).to_dict()
        agreement = None
        if heuristic["score"] is not None and model["score"] is not None:
            agreement = abs(heuristic["score"] - model["score"]) <= AGREEMENT_BAND

        headline = model["score"] if model["score"] is not None else heuristic["score"]
        return {
            "score": headline,
            "verdict": model["verdict"],
            "justification": model["justification"],
            "unsupported": model["unsupported"],
            "judged_by": "composite",
            "heuristic": heuristic,
            "model": model,
            "agreement": agreement,
        }


def build_judge(mode: str, *, client: Any = None, model_id: str = "") -> Any:
    """`mode` is one of: none, heuristic, bedrock, both."""
    mode = (mode or "heuristic").lower()
    if mode == "none":
        return None
    if mode == "heuristic":
        return CompositeJudge(HeuristicJudge(), None)
    if mode in ("bedrock", "llm", "model"):
        if client is None:
            raise ValueError("judge mode 'bedrock' needs a bedrock-runtime client")
        return CompositeJudge(HeuristicJudge(), BedrockJudge(client, model_id))
    if mode == "both":
        if client is None:
            raise ValueError("judge mode 'both' needs a bedrock-runtime client")
        return CompositeJudge(HeuristicJudge(), BedrockJudge(client, model_id))
    raise ValueError(f"Unknown judge mode '{mode}'. Use: none, heuristic, bedrock, both.")
