"""The five tools, against moto.

Grouped by tool. The bias throughout is towards the awkward inputs -- an empty
result set, a value on the wrong side of a boundary, an argument of the wrong
type -- because the happy path is the one that gets exercised by every other
test in the suite anyway.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent import tools
from agent.tools import (
    ToolInputError,
    alert_fingerprint,
    dispatch,
    normalise_metric,
)


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------


class TestValidation:
    def test_missing_argument_is_a_recoverable_error(self, ctx):
        payload, is_error = dispatch(ctx, "get_service_metrics", {"service": "checkout-api"})
        assert is_error
        assert payload["error_kind"] == "invalid_arguments"
        assert "metric" in payload["error"]
        # The failed call is still traced: the eval must see what was attempted.
        assert ctx.trace[-1].name == "get_service_metrics"
        assert ctx.trace[-1].ok is False

    def test_unknown_tool_lists_the_real_ones(self, ctx):
        payload, is_error = dispatch(ctx, "delete_production", {})
        assert is_error
        assert payload["error_kind"] == "unknown_tool"
        assert "get_service_metrics" in payload["error"]

    def test_unexpected_argument_is_rejected(self, ctx):
        payload, is_error = dispatch(
            ctx,
            "get_recent_deploys",
            {"service": "checkout-api", "hours": 24, "region": "eu-west-1"},
        )
        assert is_error
        assert "region" in payload["error"]

    def test_numeric_strings_are_coerced(self, ctx, seeded_metrics):
        seeded_metrics("checkout-api", "CPUUtilization", [50.0] * 10)
        payload, is_error = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": "30"},
        )
        assert not is_error
        assert payload["window_minutes"] == 30

    def test_non_integer_float_is_rejected(self, ctx):
        payload, is_error = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "x", "metric": "y", "window_minutes": 30.5},
        )
        assert is_error
        assert "whole number" in payload["error"]

    def test_boolean_is_not_an_integer(self, ctx):
        payload, is_error = dispatch(
            ctx, "get_recent_deploys", {"service": "x", "hours": True}
        )
        assert is_error
        assert "boolean" in payload["error"]

    @pytest.mark.parametrize("window", [0, 4, 1441, -10])
    def test_window_outside_bounds_is_rejected(self, ctx, window):
        payload, is_error = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "x", "metric": "CPUUtilization", "window_minutes": window},
        )
        assert is_error
        assert "between 5 and 1440" in payload["error"]

    def test_whitespace_only_string_is_rejected(self, ctx):
        payload, is_error = dispatch(ctx, "search_runbook", {"symptom": "   "})
        assert is_error
        assert "must not be empty" in payload["error"]

    def test_dispatch_never_raises_on_a_broken_tool(self, ctx, monkeypatch):
        def explode(_ctx, _args):
            raise RuntimeError("something in our own code broke")

        monkeypatch.setitem(
            tools._HANDLERS, "search_runbook", (explode, {"symptom"})
        )
        payload, is_error = dispatch(ctx, "search_runbook", {"symptom": "cpu"})
        assert is_error
        assert payload["error_kind"] == "internal_error"


# --------------------------------------------------------------------------
# get_service_metrics
# --------------------------------------------------------------------------


class TestGetServiceMetrics:
    def test_returns_summary_and_datapoints(self, ctx, seeded_metrics):
        seeded_metrics("checkout-api", "CPUUtilization", [40, 45, 50, 90, 92, 94])
        payload, is_error = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": 30},
        )
        assert not is_error
        assert payload["datapoint_count"] == 6
        assert payload["summary"]["min"] == 40.0
        assert payload["summary"]["max"] == 94.0
        assert payload["summary"]["latest"] == 94.0
        assert payload["summary"]["first"] == 40.0
        assert payload["summary"]["trend"] == "rising"

    def test_no_data_says_so_explicitly(self, ctx):
        payload, is_error = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": 60},
        )
        assert not is_error
        assert payload["datapoint_count"] == 0
        assert payload["summary"] is None
        # The wording matters: an empty result must not read as "healthy".
        assert "not that the value is zero" in payload["note"]
        assert "CPUUtilization" in payload["known_metrics_for_service"]

    def test_datapoints_are_sorted_regardless_of_api_order(self, ctx, seeded_metrics, monkeypatch):
        seeded_metrics("checkout-api", "CPUUtilization", [10, 20, 30, 40])
        real = ctx.cw.get_metric_statistics

        def shuffled(**kwargs):
            response = real(**kwargs)
            response["Datapoints"] = list(reversed(response["Datapoints"]))
            return response

        monkeypatch.setattr(ctx.cw, "get_metric_statistics", shuffled)
        payload, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": 30},
        )
        stamps = [point["t"] for point in payload["datapoints"]]
        assert stamps == sorted(stamps)
        assert payload["summary"]["latest"] == 40.0

    def test_metric_alias_is_normalised_and_reported(self, ctx, seeded_metrics):
        seeded_metrics("checkout-api", "CPUUtilization", [55, 56])
        payload, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "cpu", "window_minutes": 30},
        )
        assert payload["metric"] == "CPUUtilization"
        assert payload["normalised_from"] == "cpu"
        assert payload["datapoint_count"] == 2

    def test_output_is_truncated_to_the_most_recent_points(self, ctx, seeded_metrics):
        # 90 minutes is the largest window that still uses a 60-second period,
        # so 90 one-minute datapoints survive aggregation and the cap bites.
        # A 120-minute window would aggregate to 5-minute buckets and return
        # only 24 points, never reaching the cap at all.
        seeded_metrics("checkout-api", "CPUUtilization", list(range(100)))
        payload, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "checkout-api", "metric": "CPUUtilization", "window_minutes": 90},
        )
        assert payload["truncated"] is True
        assert payload["datapoint_count"] == ctx.cfg.max_datapoints_returned
        # The tail is kept, because that is what the decision hinges on.
        assert payload["summary"]["latest"] == 99.0

    @pytest.mark.parametrize(
        "window,period", [(30, 60), (90, 60), (91, 300), (360, 300), (361, 900), (1440, 900)]
    )
    def test_period_scales_with_window(self, ctx, window, period):
        payload, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "x", "metric": "CPUUtilization", "window_minutes": window},
        )
        assert payload["period_seconds"] == period

    def test_trend_flat_and_falling(self, ctx, seeded_metrics):
        seeded_metrics("svc-flat", "CPUUtilization", [50, 50, 50, 50, 50, 50])
        flat, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "svc-flat", "metric": "CPUUtilization", "window_minutes": 30},
        )
        assert flat["summary"]["trend"] == "flat"

        seeded_metrics("svc-fall", "CPUUtilization", [90, 88, 70, 40, 20, 10])
        falling, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "svc-fall", "metric": "CPUUtilization", "window_minutes": 30},
        )
        assert falling["summary"]["trend"] == "falling"

    def test_too_few_points_reports_insufficient_data(self, ctx, seeded_metrics):
        seeded_metrics("svc-thin", "CPUUtilization", [50, 90])
        payload, _ = dispatch(
            ctx,
            "get_service_metrics",
            {"service": "svc-thin", "metric": "CPUUtilization", "window_minutes": 30},
        )
        assert payload["summary"]["trend"] == "insufficient_data"


# --------------------------------------------------------------------------
# get_recent_deploys
# --------------------------------------------------------------------------


class TestGetRecentDeploys:
    def test_newest_first_with_minutes_ago(self, ctx, seeded_deploys):
        seeded_deploys("checkout-api", [180, 20, 90])
        payload, is_error = dispatch(
            ctx, "get_recent_deploys", {"service": "checkout-api", "hours": 24}
        )
        assert not is_error
        assert payload["deploy_count"] == 3
        assert [d["minutes_ago"] for d in payload["deploys"]] == [20, 90, 180]

    def test_window_excludes_older_deploys(self, ctx, seeded_deploys):
        seeded_deploys("checkout-api", [30, 60 * 30])  # 30 min and 30 hours ago
        payload, _ = dispatch(
            ctx, "get_recent_deploys", {"service": "checkout-api", "hours": 24}
        )
        assert payload["deploy_count"] == 1
        assert payload["deploys"][0]["minutes_ago"] == 30

    def test_no_deploys_says_the_alert_is_unexplained(self, ctx):
        payload, _ = dispatch(
            ctx, "get_recent_deploys", {"service": "checkout-api", "hours": 24}
        )
        assert payload["deploy_count"] == 0
        assert "not explained by a recent release" in payload["note"]

    def test_other_services_are_not_returned(self, ctx, seeded_deploys):
        seeded_deploys("payments-api", [10])
        payload, _ = dispatch(
            ctx, "get_recent_deploys", {"service": "checkout-api", "hours": 24}
        )
        assert payload["deploy_count"] == 0

    def test_lookback_is_capped(self, ctx):
        payload, is_error = dispatch(
            ctx, "get_recent_deploys", {"service": "checkout-api", "hours": 999}
        )
        assert is_error
        assert "between 1 and 168" in payload["error"]


# --------------------------------------------------------------------------
# search_runbook
# --------------------------------------------------------------------------


class TestSearchRunbook:
    def test_matches_by_keyword_and_ranks(self, ctx, seeded_runbooks):
        payload, is_error = dispatch(
            ctx, "search_runbook", {"symptom": "sustained high cpu utilisation on an api"}
        )
        assert not is_error
        assert payload["match_count"] >= 1
        assert payload["matches"][0]["runbook_id"] == "RB-001"
        assert payload["matches"][0]["steps"]

    def test_returns_at_most_three(self, ctx, seeded_runbooks):
        payload, _ = dispatch(
            ctx, "search_runbook", {"symptom": "cpu latency error queue database memory deploy"}
        )
        assert payload["match_count"] <= 3

    def test_scores_are_descending(self, ctx, seeded_runbooks):
        payload, _ = dispatch(
            ctx, "search_runbook", {"symptom": "elevated p99 latency following a deploy"}
        )
        scores = [m["match_score"] for m in payload["matches"]]
        assert scores == sorted(scores, reverse=True)

    def test_no_match_forbids_invention(self, ctx, seeded_runbooks):
        payload, _ = dispatch(
            ctx, "search_runbook", {"symptom": "zzzz qqqq wwww unrelatedgibberish"}
        )
        assert payload["match_count"] == 0
        assert "Do not invent remediation steps" in payload["note"]

    def test_flapping_symptom_finds_the_flapping_runbook(self, ctx, seeded_runbooks):
        payload, _ = dispatch(
            ctx, "search_runbook", {"symptom": "known flapping alarm oscillating across threshold"}
        )
        assert payload["matches"][0]["runbook_id"] == "RB-008"
        assert "Do not page" in payload["matches"][0]["page_guidance"]


# --------------------------------------------------------------------------
# page_oncall
# --------------------------------------------------------------------------


class TestPageOncall:
    def test_refuses_an_unknown_incident_id(self, ctx):
        payload, is_error = dispatch(
            ctx, "page_oncall", {"incident_id": "INC-FAKE", "reason": "looks bad"}
        )
        assert is_error
        assert "must call create_incident first" in payload["error"]
        assert ctx.pages_sent == []

    def test_dry_run_records_but_does_not_publish(self, ctx):
        dispatch(ctx, "create_incident", {"severity": "SEV2", "summary": "cpu high"})
        incident_id = ctx.incidents_created[0]
        payload, is_error = dispatch(
            ctx, "page_oncall", {"incident_id": incident_id, "reason": "sustained breach"}
        )
        assert not is_error
        assert payload["dry_run"] is True
        assert len(ctx.pages_sent) == 1

    def test_publishes_to_sns_when_armed(self, ctx, aws_stack):
        import dataclasses

        topic = aws_stack["sns"].create_topic(Name="triage-pages")["TopicArn"]
        ctx.cfg = dataclasses.replace(ctx.cfg, dry_run=False, sns_topic_arn=topic)

        dispatch(ctx, "create_incident", {"severity": "SEV1", "summary": "5xx spike"})
        payload, is_error = dispatch(
            ctx,
            "page_oncall",
            {"incident_id": ctx.incidents_created[0], "reason": "checkout is failing"},
        )
        assert not is_error
        assert payload["dry_run"] is False
        assert payload["message_id"]

    def test_subject_is_truncated_to_the_sns_limit(self, ctx, aws_stack):
        import dataclasses

        topic = aws_stack["sns"].create_topic(Name="triage-pages")["TopicArn"]
        ctx.cfg = dataclasses.replace(ctx.cfg, dry_run=False, sns_topic_arn=topic)
        ctx.alert = dict(ctx.alert, service="s" * 200)

        dispatch(ctx, "create_incident", {"severity": "SEV1", "summary": "x"})
        payload, is_error = dispatch(
            ctx, "page_oncall", {"incident_id": ctx.incidents_created[0], "reason": "y"}
        )
        # SNS rejects a Subject over 100 characters with an InvalidParameter
        # error, so the truncation is load-bearing, not cosmetic.
        assert not is_error, payload
        assert ctx.pages_sent[-1]["subject"]


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------


class TestFingerprint:
    def test_is_stable_across_calls(self, alert):
        assert alert_fingerprint(alert) == alert_fingerprint(dict(alert))

    def test_ignores_the_reading(self, alert):
        """A storm is the same alarm re-firing with a different value each time.

        If the value were part of the identity, every re-fire would look new and
        the dedupe would never trigger -- the exact bug it exists to prevent.
        """
        louder = dict(alert, value=99.0, duration_min=40, alert_id="different")
        assert alert_fingerprint(louder) == alert_fingerprint(alert)

    def test_is_case_and_whitespace_insensitive(self, alert):
        messy = dict(alert, service="  CHECKOUT-API  ", metric="cpuutilization")
        # metric case is normalised in the fingerprint, so these collapse.
        assert alert_fingerprint(messy) == alert_fingerprint(
            dict(alert, metric="cpuutilization")
        )

    def test_environment_separates_prod_from_dev(self, alert):
        assert alert_fingerprint(dict(alert, environment="dev")) != alert_fingerprint(alert)

    def test_different_alarms_differ(self, alert):
        assert alert_fingerprint(dict(alert, alarm_name="other")) != alert_fingerprint(alert)
        assert alert_fingerprint(dict(alert, service="payments-api")) != alert_fingerprint(alert)


class TestMetricNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("cpu", "CPUUtilization"),
            ("CPU", "CPUUtilization"),
            ("cpu_utilization", "CPUUtilization"),
            ("CPU-Utilization", "CPUUtilization"),
            ("p99", "Latencyp99"),
            ("5xx", "Error5xxRate"),
            ("queue_depth", "QueueDepth"),
            ("CPUUtilization", "CPUUtilization"),
        ],
    )
    def test_aliases(self, raw, expected):
        assert normalise_metric(raw) == expected

    def test_unknown_names_pass_through(self):
        assert normalise_metric("SomeCustomMetric") == "SomeCustomMetric"


# --------------------------------------------------------------------------
# DynamoDB item deserialisation
# --------------------------------------------------------------------------


class TestDeserialiseItem:
    def test_handles_raw_wire_format(self):
        """The item returned by ReturnValuesOnConditionCheckFailure arrives in
        wire format even through the resource layer, which only deserialises
        successful responses."""
        wire = {"incident_id": {"S": "INC-1"}, "opened_at_epoch": {"N": "1000"}}
        assert tools.deserialise_item(wire) == {
            "incident_id": "INC-1",
            "opened_at_epoch": 1000,
        }

    def test_passes_through_already_deserialised_items(self):
        from decimal import Decimal

        plain = {"incident_id": "INC-1", "opened_at_epoch": Decimal("1000")}
        assert tools.deserialise_item(plain) == {
            "incident_id": "INC-1",
            "opened_at_epoch": 1000,
        }

    def test_empty_and_none_are_safe(self):
        assert tools.deserialise_item(None) == {}
        assert tools.deserialise_item({}) == {}
