"""Groundedness judges.

The heuristic judge has to walk a line: strict enough to catch an invented
figure, lenient enough not to flag correct arithmetic or a sensibly rounded
reading. Both directions are failures -- a judge that flags everything is as
useless as one that flags nothing -- so both directions are tested.

The known weakness is pinned too: reasoning that cites no numbers at all scores
as grounded. That is documented rather than fixed, because the alternative
(penalising unquantified reasoning) would conflate "vague" with "fabricated",
and those need different responses.
"""

from __future__ import annotations

import json

import pytest

from agent.agent import TriageResult
from agent.tools import ToolCall
from eval.judge import (
    BedrockJudge,
    CompositeJudge,
    HeuristicJudge,
    build_judge,
    compact_trace,
    extract_numbers,
)


def result_with(reasoning, *, metrics=None, runbook_cited=None, trace=None, alert=None):
    calls = list(trace or [])
    if metrics is not None:
        calls.append(
            ToolCall(
                name="get_service_metrics",
                arguments={"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": 60},
                ok=True,
                result=metrics,
            )
        )
    return TriageResult(
        alert=alert or {"service": "checkout-api", "value": 94.0, "threshold": 80.0, "duration_min": 12},
        correlation_id="t",
        reasoning=reasoning,
        runbook_cited=runbook_cited,
        trace=calls,
    )


METRICS = {
    "service": "checkout-api",
    "metric": "CPUUtilization",
    "window_minutes": 60,
    "summary": {"avg": 91.2, "max": 96.4, "min": 40.1, "latest": 94.0, "trend": "flat"},
    "datapoints": [{"t": "2026-09-04T11:00:00Z", "avg": 91.2}],
}


class TestExtractNumbers:
    def test_plain_numbers(self):
        assert extract_numbers("cpu was 94 and latency 1200") == [94.0, 1200.0]

    def test_decimals_and_percentages(self):
        assert extract_numbers("error rate 8.4% of 100") == [8.4, 100.0]

    @pytest.mark.parametrize(
        "text",
        ["5xx errors", "p99 latency", "SEV2 incident", "version v4.11.0", "INC-20260904-ABCD1234", "runbook RB-003"],
    )
    def test_identifiers_are_not_quantities(self, text):
        """Counting the 99 in p99 as an unsupported figure would make the score
        a measure of vocabulary rather than honesty."""
        assert extract_numbers(text) == []

    def test_iso_timestamps_are_ignored(self):
        assert extract_numbers("at 2026-09-04T11:00:00Z the value was 94") == [94.0]

    def test_empty(self):
        assert extract_numbers("") == []


class TestHeuristicJudge:
    judge = HeuristicJudge()

    def test_grounded_reasoning_scores_one(self):
        result = result_with(
            "CPU averaged 91.2 with a peak of 96.4 and the latest reading is 94.0, "
            "against a threshold of 80.",
            metrics=METRICS,
        )
        verdict = self.judge.judge({}, result)
        assert verdict.score == 1.0
        assert verdict.verdict == "grounded"
        assert verdict.unsupported == []

    def test_invented_figure_is_caught(self):
        result = result_with(
            "CPU has been pinned at 99.7% for 41 minutes.", metrics=METRICS
        )
        verdict = self.judge.judge({}, result)
        assert verdict.score < 1.0
        assert verdict.verdict == "ungrounded"
        assert any("99.7" in claim for claim in verdict.unsupported)

    def test_sensible_rounding_is_not_a_fabrication(self):
        """96.4 read off a graph as 96 is a correct reading, not a lie."""
        result = result_with("CPU peaked at 96 percent.", metrics=METRICS)
        assert self.judge.judge({}, result).score == 1.0

    def test_alert_values_count_as_grounded(self):
        result = result_with(
            "The alarm fired at 94.0 against a threshold of 80.0 after 12 minutes."
        )
        assert self.judge.judge({}, result).score == 1.0

    def test_invented_runbook_id_is_caught(self):
        trace = [
            ToolCall(
                name="search_runbook",
                arguments={"symptom": "cpu"},
                ok=True,
                result={"matches": [{"runbook_id": "RB-001"}]},
            )
        ]
        result = result_with("Following the runbook.", trace=trace, runbook_cited="RB-999")
        verdict = self.judge.judge({}, result)
        assert verdict.score < 1.0
        assert any("RB-999" in claim for claim in verdict.unsupported)

    def test_a_real_runbook_id_is_accepted(self):
        trace = [
            ToolCall(
                name="search_runbook",
                arguments={"symptom": "cpu"},
                ok=True,
                result={"matches": [{"runbook_id": "RB-001"}]},
            )
        ]
        result = result_with("Following the runbook.", trace=trace, runbook_cited="RB-001")
        assert self.judge.judge({}, result).unsupported == []

    def test_claiming_deploy_evidence_without_calling_the_tool(self):
        result = result_with(
            "This correlates with a recent deploy that went out shortly before.",
            metrics=METRICS,
        )
        verdict = self.judge.judge({}, result)
        assert any("get_recent_deploys" in claim for claim in verdict.unsupported)

    def test_claiming_runbook_evidence_without_calling_the_tool(self):
        result = result_with("The runbook says to roll back.", metrics=METRICS)
        verdict = self.judge.judge({}, result)
        assert any("search_runbook" in claim for claim in verdict.unsupported)

    def test_no_reasoning_scores_none_not_zero(self):
        verdict = self.judge.judge({}, result_with(""))
        assert verdict.score is None
        assert verdict.verdict == "no_reasoning"

    def test_numberless_reasoning_is_a_known_blind_spot(self):
        """Documented, not fixed: 'vague' and 'fabricated' need different
        responses, and the LLM judge is what covers this gap."""
        verdict = self.judge.judge({}, result_with("Something seems wrong here."))
        assert verdict.score == 1.0
        assert verdict.verdict == "no_figures_cited"

    def test_score_is_deterministic(self):
        result = result_with("CPU averaged 91.2 and peaked at 96.4.", metrics=METRICS)
        scores = {self.judge.judge({}, result).score for _ in range(5)}
        assert len(scores) == 1


class TestCompactTrace:
    def test_long_series_is_sampled_from_both_ends(self):
        points = [{"t": f"t{i}", "avg": i} for i in range(50)]
        result = result_with("x", metrics={**METRICS, "datapoints": points})
        compact = compact_trace(result, max_datapoints=6)
        series = compact[0]["result"]["datapoints"]
        assert len(series) == 7  # 3 head + marker + 3 tail
        assert series[0]["avg"] == 0
        assert series[-1]["avg"] == 49
        assert "omitted" in series[3]["note"]

    def test_summary_survives_compaction(self):
        result = result_with("x", metrics=METRICS)
        assert compact_trace(result)[0]["result"]["summary"]["max"] == 96.4

    def test_failed_calls_keep_their_error(self):
        trace = [ToolCall(name="search_runbook", arguments={}, ok=False, error="boom")]
        compact = compact_trace(result_with("x", trace=trace))
        assert compact[0]["error"] == "boom"


class FakeJudgeClient:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}}}


class TestBedrockJudge:
    def test_parses_a_verdict(self):
        client = FakeJudgeClient(
            json.dumps(
                {
                    "score": 0.4,
                    "verdict": "partially_grounded",
                    "justification": "One figure was not returned by any tool.",
                    "unsupported_claims": ["pinned at 99.7%"],
                }
            )
        )
        verdict = BedrockJudge(client, "model-x").judge({}, result_with("x", metrics=METRICS))
        assert verdict.score == 0.4
        assert verdict.unsupported == ["pinned at 99.7%"]
        # Temperature 0: a judge that answers differently on Tuesday is not a
        # measurement.
        assert client.calls[0]["inferenceConfig"]["temperature"] == 0.0

    def test_score_is_clamped(self):
        client = FakeJudgeClient('{"score": 7, "verdict": "grounded"}')
        assert BedrockJudge(client, "m").judge({}, result_with("x", metrics=METRICS)).score == 1.0

    def test_unparseable_output_scores_none(self):
        """The harness failing must not be reported as the agent failing."""
        verdict = BedrockJudge(FakeJudgeClient("I think it's fine?"), "m").judge(
            {}, result_with("x", metrics=METRICS)
        )
        assert verdict.score is None
        assert verdict.verdict == "judge_unparseable"

    def test_client_failure_scores_none(self):
        class Broken:
            def converse(self, **kwargs):
                raise RuntimeError("throttled")

        verdict = BedrockJudge(Broken(), "m").judge({}, result_with("x", metrics=METRICS))
        assert verdict.score is None
        assert verdict.verdict == "judge_error"

    def test_the_judge_never_sees_the_expected_answer(self):
        """A judge told the right answer rationalises towards it."""
        client = FakeJudgeClient('{"score": 1.0, "verdict": "grounded"}')
        case = {"expected_page": True, "reference_reasoning": "PAGE because X"}
        BedrockJudge(client, "m").judge(case, result_with("x", metrics=METRICS))
        sent = json.dumps(client.calls[0], default=str)
        assert "reference_reasoning" not in sent
        assert "PAGE because X" not in sent
        assert "expected_page" not in sent


class TestCompositeJudge:
    def test_heuristic_only(self):
        verdict = CompositeJudge(HeuristicJudge(), None).judge(
            {}, result_with("CPU averaged 91.2.", metrics=METRICS)
        )
        assert verdict["score"] == 1.0
        assert verdict["model"] is None
        assert verdict["agreement"] is None

    def test_agreement_when_both_agree(self):
        judge = CompositeJudge(
            HeuristicJudge(), BedrockJudge(FakeJudgeClient('{"score": 0.95}'), "m")
        )
        verdict = judge.judge({}, result_with("CPU averaged 91.2.", metrics=METRICS))
        assert verdict["agreement"] is True
        assert verdict["score"] == 0.95  # the LLM judge is the headline

    def test_disagreement_is_visible(self):
        judge = CompositeJudge(
            HeuristicJudge(), BedrockJudge(FakeJudgeClient('{"score": 0.1}'), "m")
        )
        verdict = judge.judge({}, result_with("CPU averaged 91.2.", metrics=METRICS))
        assert verdict["agreement"] is False
        assert verdict["heuristic"]["score"] == 1.0

    def test_falls_back_to_the_heuristic_when_the_judge_breaks(self):
        judge = CompositeJudge(
            HeuristicJudge(), BedrockJudge(FakeJudgeClient("garbage"), "m")
        )
        verdict = judge.judge({}, result_with("CPU averaged 91.2.", metrics=METRICS))
        assert verdict["score"] == 1.0
        assert verdict["model"]["verdict"] == "judge_unparseable"


class TestBuildJudge:
    def test_none_disables_judging(self):
        assert build_judge("none") is None

    def test_heuristic_needs_no_client(self):
        assert isinstance(build_judge("heuristic"), CompositeJudge)

    def test_bedrock_without_a_client_fails_loudly(self):
        with pytest.raises(ValueError, match="bedrock-runtime client"):
            build_judge("bedrock")

    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown judge mode"):
            build_judge("vibes")
