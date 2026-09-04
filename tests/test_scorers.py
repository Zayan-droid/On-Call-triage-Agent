"""The four metrics.

The most important assertions here are the ones about what a metric must NOT
do: report an undefined ratio as zero, double-count one mistake across two
metrics, or punish behaviour that is merely unnecessary rather than wrong. Those
are the ways an eval reports a confident number that means the wrong thing.
"""

from __future__ import annotations

import pytest

from agent.tools import ToolCall
from eval import scorers


def call(name, **arguments):
    return ToolCall(name=name, arguments=arguments, ok=True, result={})


# --------------------------------------------------------------------------
# Metric 1
# --------------------------------------------------------------------------


class TestToolSelection:
    CASE = {"expected_tools": ["get_service_metrics", "get_recent_deploys"]}

    def test_exact_match_when_all_required_are_called(self):
        score = scorers.score_tool_selection(
            self.CASE, ["get_service_metrics", "get_recent_deploys"]
        )
        assert score["exact_match"] is True
        assert score["recall"] == 1.0
        assert score["precision"] == 1.0

    def test_missing_a_required_tool_fails(self):
        score = scorers.score_tool_selection(self.CASE, ["get_service_metrics"])
        assert score["exact_match"] is False
        assert score["missing"] == ["get_recent_deploys"]
        assert score["recall"] == 0.5

    def test_extra_read_only_investigation_is_not_an_error(self):
        """Over-investigating costs tokens, not correctness. Grading it wrong
        would push the agent to investigate less, which is the failure this
        whole project exists to measure."""
        score = scorers.score_tool_selection(
            self.CASE,
            ["get_service_metrics", "get_recent_deploys", "search_runbook"],
        )
        assert score["exact_match"] is True
        assert score["extra_calls"] == ["search_runbook"]

    def test_action_tools_are_invisible_to_this_metric(self):
        """Escalation is metric 4's job. Counting page_oncall here too would
        make two supposedly independent metrics move together."""
        score = scorers.score_tool_selection(
            self.CASE,
            ["get_service_metrics", "get_recent_deploys", "create_incident", "page_oncall"],
        )
        assert score["exact_match"] is True
        assert "page_oncall" not in score["called"]

    def test_forbidden_tool_fails_the_case(self):
        case = dict(self.CASE, forbidden_tools=["search_runbook"])
        score = scorers.score_tool_selection(
            case, ["get_service_metrics", "get_recent_deploys", "search_runbook"]
        )
        assert score["exact_match"] is False
        assert score["forbidden_called"] == ["search_runbook"]

    def test_no_investigation_at_all(self):
        score = scorers.score_tool_selection(self.CASE, ["page_oncall"])
        assert score["investigated_at_all"] is False
        assert score["recall"] == 0.0
        assert score["exact_match"] is False

    def test_repeated_calls_are_counted_for_cost(self):
        score = scorers.score_tool_selection(
            self.CASE,
            ["get_service_metrics", "get_service_metrics", "get_recent_deploys"],
        )
        assert score["total_investigation_calls"] == 3
        assert score["exact_match"] is True

    def test_a_case_requiring_nothing_is_satisfied_by_anything(self):
        score = scorers.score_tool_selection({"expected_tools": []}, [])
        assert score["exact_match"] is True
        assert score["recall"] == 1.0


# --------------------------------------------------------------------------
# Metric 2
# --------------------------------------------------------------------------


class TestToolParameters:
    CASE = {
        "expected_tools": ["get_service_metrics"],
        "expected_params": {
            "get_service_metrics": {
                "service": {"equals": "checkout-api"},
                "metric": {"equals": "CPUUtilization"},
                "window_minutes": {"min": 24, "max": 480},
            }
        },
    }

    def test_all_correct(self):
        trace = [call("get_service_metrics", service="checkout-api", metric="CPUUtilization", window_minutes=60)]
        score = scorers.score_tool_parameters(self.CASE, trace)
        assert score["accuracy"] == 1.0
        assert score["coverage"] == 1.0
        assert score["failures"] == []

    def test_wrong_service_is_caught(self):
        trace = [call("get_service_metrics", service="checkout", metric="CPUUtilization", window_minutes=60)]
        score = scorers.score_tool_parameters(self.CASE, trace)
        assert score["accuracy"] == pytest.approx(2 / 3)
        assert "checkout" in score["failures"][0]

    def test_window_outside_range_is_caught(self):
        trace = [call("get_service_metrics", service="checkout-api", metric="CPUUtilization", window_minutes=5)]
        score = scorers.score_tool_parameters(self.CASE, trace)
        assert score["accuracy"] == pytest.approx(2 / 3)

    def test_metric_alias_counts_as_correct(self):
        """`cpu` and `CPUUtilization` produce an identical CloudWatch call, so
        scoring them differently would measure spelling, not tool use."""
        trace = [call("get_service_metrics", service="checkout-api", metric="cpu", window_minutes=60)]
        assert scorers.score_tool_parameters(self.CASE, trace)["accuracy"] == 1.0

    def test_uncalled_tool_scores_none_not_zero(self):
        """Metric 1 already counted the missing call. Counting it again here
        would report one mistake twice and make metric 2 a noisy copy of 1."""
        score = scorers.score_tool_parameters(self.CASE, [])
        assert score["accuracy"] is None
        assert score["coverage"] == 0.0
        assert score["checks_total"] == 0

    def test_the_best_attempt_is_scored(self):
        """An agent that fixes its own bad argument should not be punished."""
        trace = [
            call("get_service_metrics", service="wrong", metric="cpu", window_minutes=1),
            call("get_service_metrics", service="checkout-api", metric="CPUUtilization", window_minutes=60),
        ]
        score = scorers.score_tool_parameters(self.CASE, trace)
        assert score["accuracy"] == 1.0
        assert score["checks"][0]["attempts"] == 2

    def test_missing_argument_fails_that_check(self):
        trace = [call("get_service_metrics", service="checkout-api", metric="CPUUtilization")]
        score = scorers.score_tool_parameters(self.CASE, trace)
        assert score["accuracy"] == pytest.approx(2 / 3)
        assert "not supplied" in score["failures"][0]

    def test_contains_any_comparator(self):
        case = {
            "expected_tools": ["search_runbook"],
            "expected_params": {"search_runbook": {"symptom": {"contains_any": ["flap", "noisy"]}}},
        }
        hit = [call("search_runbook", symptom="known flapping alarm")]
        miss = [call("search_runbook", symptom="high cpu")]
        assert scorers.score_tool_parameters(case, hit)["accuracy"] == 1.0
        assert scorers.score_tool_parameters(case, miss)["accuracy"] == 0.0

    def test_one_of_comparator(self):
        case = {
            "expected_tools": ["create_incident"],
            "expected_params": {"create_incident": {"severity": {"one_of": ["SEV1", "SEV2"]}}},
        }
        assert scorers.score_tool_parameters(case, [call("create_incident", severity="SEV2")])["accuracy"] == 1.0
        assert scorers.score_tool_parameters(case, [call("create_incident", severity="SEV4")])["accuracy"] == 0.0

    def test_unknown_comparator_fails_loudly(self):
        case = {
            "expected_tools": ["search_runbook"],
            "expected_params": {"search_runbook": {"symptom": {"regex": "x"}}},
        }
        with pytest.raises(ValueError, match="Unknown comparator"):
            scorers.score_tool_parameters(case, [call("search_runbook", symptom="x")])


# --------------------------------------------------------------------------
# Metric 4
# --------------------------------------------------------------------------


class TestEscalation:
    @pytest.mark.parametrize(
        "expected,actual,label",
        [(True, True, "TP"), (True, False, "FN"), (False, True, "FP"), (False, False, "TN")],
    )
    def test_classification(self, expected, actual, label):
        assert scorers.classify_escalation(expected, actual) == label

    def test_aggregate_arithmetic(self):
        agg = scorers.aggregate_escalation(["TP"] * 8 + ["FP"] * 2 + ["TN"] * 8 + ["FN"] * 2)
        assert agg["precision"] == pytest.approx(0.8)
        assert agg["recall"] == pytest.approx(0.8)
        assert agg["f1"] == pytest.approx(0.8)
        assert agg["accuracy"] == pytest.approx(0.8)
        assert agg["false_page_rate"] == pytest.approx(0.2)
        assert agg["missed_page_rate"] == pytest.approx(0.2)

    def test_false_page_rate_is_not_one_minus_precision(self):
        """FPR is over the negatives; precision is over the positives. They
        answer different questions and move independently -- which is why the
        alarm is on FPR, not on precision."""
        agg = scorers.aggregate_escalation(["TP"] * 50 + ["FP"] * 5 + ["TN"] * 5)
        assert agg["precision"] == pytest.approx(50 / 55)
        assert agg["false_page_rate"] == pytest.approx(0.5)

    def test_perfect_silence_does_not_look_like_failure(self):
        """A run where nothing should page and nothing did is a perfect run.
        Reporting precision as 0.0 here would call it a total failure."""
        agg = scorers.aggregate_escalation(["TN"] * 10)
        assert agg["precision"] is None
        assert agg["recall"] is None
        assert agg["accuracy"] == 1.0
        assert agg["false_page_rate"] == 0.0
        assert agg["weighted_cost"] == 0.0

    def test_weighted_cost_prices_a_miss_above_a_false_page(self):
        one_miss = scorers.aggregate_escalation(["FN"] + ["TN"] * 9)
        one_false = scorers.aggregate_escalation(["FP"] + ["TN"] * 9)
        assert one_miss["weighted_cost"] > one_false["weighted_cost"]
        assert one_miss["weighted_cost"] == pytest.approx(0.5)
        assert one_false["weighted_cost"] == pytest.approx(0.1)

    def test_weights_are_reported_alongside_the_number(self):
        agg = scorers.aggregate_escalation(["TP"], miss_weight=3.0, false_page_weight=2.0)
        assert agg["weights"] == {"miss": 3.0, "false_page": 2.0}

    def test_empty_input(self):
        agg = scorers.aggregate_escalation([])
        assert agg["n"] == 0
        assert agg["precision"] is None
        assert agg["accuracy"] is None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def make_score(case_id, bucket, escalation, *, exact=True, accuracy=1.0, grounded=1.0):
    return scorers.CaseScore(
        case_id=case_id,
        bucket=bucket,
        expected_page=escalation in ("TP", "FN"),
        actual_paged=escalation in ("TP", "FP"),
        decision="PAGE" if escalation in ("TP", "FP") else "NOISE",
        escalation=escalation,
        tool_selection={
            "exact_match": exact,
            "f1": 1.0,
            "recall": 1.0,
            "missing": [] if exact else ["get_service_metrics"],
            "forbidden_called": [],
            "investigated_at_all": True,
            "total_investigation_calls": 2,
        },
        tool_parameters={"accuracy": accuracy, "coverage": 1.0, "failures": []},
        groundedness={"score": grounded},
    )


class TestAggregate:
    def test_empty(self):
        assert scorers.aggregate([]) == {"n": 0}

    def test_splits_by_bucket(self):
        scores = [
            make_score("a", "noise", "TN"),
            make_score("b", "noise", "FP"),
            make_score("c", "clear_incident", "TP"),
        ]
        agg = scorers.aggregate(scores)
        assert agg["overall"]["n"] == 3
        assert set(agg["by_bucket"]) == {"noise", "clear_incident"}
        assert agg["by_bucket"]["noise"]["escalation"]["false_page_rate"] == pytest.approx(0.5)
        assert agg["by_bucket"]["clear_incident"]["escalation"]["recall"] == 1.0

    def test_failures_list_surfaces_every_kind_of_problem(self):
        scores = [
            make_score("good", "noise", "TN"),
            make_score("false_page", "noise", "FP"),
            make_score("missed", "clear_incident", "FN"),
            make_score("bad_tools", "noise", "TN", exact=False),
            make_score("bad_params", "noise", "TN", accuracy=0.5),
        ]
        failures = {f["case_id"] for f in scorers.aggregate(scores)["failures"]}
        assert failures == {"false_page", "missed", "bad_tools", "bad_params"}

    def test_none_scores_are_skipped_not_treated_as_zero(self):
        scores = [make_score("a", "noise", "TN", grounded=1.0), make_score("b", "noise", "TN", grounded=None)]
        agg = scorers.aggregate(scores)
        assert agg["overall"]["groundedness"] == 1.0
        assert agg["overall"]["groundedness_scored"] == 1

    def test_all_none_yields_none(self):
        scores = [make_score("a", "noise", "TN", grounded=None)]
        assert scorers.aggregate(scores)["overall"]["groundedness"] is None
