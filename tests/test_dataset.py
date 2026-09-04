"""Integrity of the test set itself, and of the world it describes.

A dataset bug is the most expensive kind in this project: it silently redefines
what "correct" means, and every score downstream is then confidently wrong. These
tests exist so that a mistake in `alerts.jsonl` fails here rather than showing
up as a mysterious regression in the escalation metric three weeks later.
"""

from __future__ import annotations

import collections
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eval import world
from eval.run import DEFAULT_DATASET, filter_cases, load_cases

BUCKETS = {"clear_incident", "needs_investigation", "multi_step_correlation", "noise"}
INVESTIGATION_TOOLS = {"get_service_metrics", "get_recent_deploys", "search_runbook"}


class TestDatasetShape:
    def test_size_is_in_the_stated_range(self, cases):
        """The README and the resume bullet both quote this number."""
        assert 35 <= len(cases) <= 40

    def test_ids_are_unique(self, cases):
        ids = [c["id"] for c in cases]
        assert len(set(ids)) == len(ids)

    def test_every_bucket_is_represented(self, cases):
        counts = collections.Counter(c["bucket"] for c in cases)
        assert set(counts) == BUCKETS
        for bucket, count in counts.items():
            assert count >= 8, f"{bucket} has only {count} cases"

    def test_page_and_no_page_are_balanced(self, cases):
        """A lopsided set makes precision and recall hard to read, and lets a
        constant predictor look good on one of them."""
        pages = sum(1 for c in cases if c["expected_page"])
        assert abs(pages - (len(cases) - pages)) <= 4

    def test_noise_bucket_never_pages(self, cases):
        """The whole point of the bucket. If one of these expects a page, the
        false-page rate stops measuring what it claims to."""
        noise = [c for c in cases if c["bucket"] == "noise"]
        assert noise
        assert all(c["expected_page"] is False for c in noise)

    def test_clear_incidents_always_page(self, cases):
        clear = [c for c in cases if c["bucket"] == "clear_incident"]
        assert all(c["expected_page"] is True for c in clear)

    def test_multi_step_cases_require_two_tools(self, cases):
        multi = [c for c in cases if c["bucket"] == "multi_step_correlation"]
        for case in multi:
            assert {"get_service_metrics", "get_recent_deploys"} <= set(case["expected_tools"]), (
                f"{case['id']} is in the correlation bucket but does not require both tools"
            )


class TestDatasetContent:
    def test_expected_tools_are_investigation_only(self, cases):
        """Action tools belong to the escalation metric. Putting one here would
        make metrics 1 and 4 measure the same thing."""
        for case in cases:
            assert set(case["expected_tools"]) <= INVESTIGATION_TOOLS, case["id"]

    def test_expected_params_only_name_expected_tools(self, cases):
        for case in cases:
            assert set(case.get("expected_params", {})) <= set(case["expected_tools"]), case["id"]

    def test_alert_metric_is_seeded(self, cases):
        """Otherwise the agent queries the alerting metric and gets nothing,
        and every case becomes a no-data case."""
        for case in cases:
            metrics = set((case.get("scenario") or {}).get("metrics", {}))
            assert case["alert"]["metric"] in metrics, case["id"]

    def test_service_metric_pairs_are_unique(self, cases):
        """CloudWatch keys a series by (namespace, metric, dimensions). Two
        cases sharing a (service, metric) pair would overwrite each other's
        world, and both would score against data meant for the other."""
        pairs = collections.Counter(
            (case["alert"]["service"], metric)
            for case in cases
            for metric in (case.get("scenario") or {}).get("metrics", {})
        )
        duplicates = {pair: n for pair, n in pairs.items() if n > 1}
        assert not duplicates, f"colliding metric series: {duplicates}"

    def test_shapes_are_known(self, cases):
        for case in cases:
            for metric, spec in (case.get("scenario") or {}).get("metrics", {}).items():
                assert spec["shape"] in world.SHAPES, f"{case['id']}/{metric}"

    def test_alert_value_actually_breaches_its_threshold(self, cases):
        """Every case represents an alarm that fired. A case whose value does
        not breach is a case the agent is right to dismiss for the wrong reason."""
        for case in cases:
            alert = case["alert"]
            value, threshold = alert.get("value"), alert.get("threshold")
            if value is None or threshold is None:
                continue
            if "Less" in alert.get("comparison", "GreaterThanThreshold"):
                assert value < threshold, case["id"]
            else:
                assert value > threshold, case["id"]

    def test_every_case_has_reference_reasoning(self, cases):
        """Not scored against -- it is the human record of why the label is
        what it is, which is what makes a disputed case arguable."""
        for case in cases:
            assert len(case.get("reference_reasoning", "")) > 40, case["id"]

    def test_window_expectations_are_satisfiable(self, cases):
        """A min above the tool's 1440 ceiling would be unscoreable."""
        for case in cases:
            rule = (case.get("expected_params", {}).get("get_service_metrics", {})).get(
                "window_minutes"
            )
            if not rule:
                continue
            assert rule["min"] >= 5 and rule["max"] <= 1440, case["id"]
            assert rule["min"] < rule["max"], case["id"]

    def test_non_prod_cases_never_expect_a_page(self, cases):
        for case in cases:
            if case["alert"].get("environment", "prod") != "prod":
                assert case["expected_page"] is False, case["id"]


class TestLoader:
    def test_rejects_a_missing_field(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps({"id": "x", "alert": {}, "expected_page": True}) + "\n")
        with pytest.raises(ValueError, match="expected_tools"):
            load_cases(path)

    def test_rejects_a_non_boolean_expected_page(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text(
            json.dumps(
                {"id": "x", "alert": {}, "expected_page": "yes", "expected_tools": []}
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="must be true or false"):
            load_cases(path)

    def test_rejects_duplicate_ids(self, tmp_path):
        case = {"id": "x", "alert": {}, "expected_page": True, "expected_tools": []}
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps(case) + "\n" + json.dumps(case) + "\n")
        with pytest.raises(ValueError, match="duplicate case id"):
            load_cases(path)

    def test_rejects_an_action_tool_in_expected_tools(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text(
            json.dumps(
                {
                    "id": "x",
                    "alert": {},
                    "expected_page": True,
                    "expected_tools": ["page_oncall"],
                }
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="action tool"):
            load_cases(path)

    def test_rejects_invalid_json_naming_the_line(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"id": "ok", "alert": {}, "expected_page": true, "expected_tools": []}\n{oops\n')
        with pytest.raises(ValueError, match=":2"):
            load_cases(path)

    def test_blank_lines_and_comments_are_skipped(self, tmp_path):
        path = tmp_path / "ok.jsonl"
        path.write_text(
            "# a comment\n\n"
            + json.dumps({"id": "x", "alert": {}, "expected_page": True, "expected_tools": []})
            + "\n"
        )
        assert len(load_cases(path)) == 1

    def test_empty_file_is_an_error(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("\n\n")
        with pytest.raises(ValueError, match="no cases"):
            load_cases(path)


class TestFilters:
    def test_by_bucket(self, cases):
        filtered = filter_cases(cases, buckets=["noise"], ids=None, limit=None)
        assert {c["bucket"] for c in filtered} == {"noise"}

    def test_by_id(self, cases):
        filtered = filter_cases(cases, buckets=None, ids=["clear_001"], limit=None)
        assert [c["id"] for c in filtered] == ["clear_001"]

    def test_unknown_id_is_an_error(self, cases):
        with pytest.raises(ValueError, match="No such case id"):
            filter_cases(cases, buckets=None, ids=["nope"], limit=None)

    def test_limit(self, cases):
        assert len(filter_cases(cases, buckets=None, ids=None, limit=3)) == 3

    def test_over_filtering_is_an_error(self, cases):
        with pytest.raises(ValueError, match="excluded every case"):
            filter_cases(cases, buckets=["does_not_exist"], ids=None, limit=None)


# --------------------------------------------------------------------------
# The world generator
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


class TestWorldGenerator:
    def test_series_are_reproducible(self):
        """Non-reproducible fixtures make a regression indistinguishable from a
        reroll."""
        spec = {"shape": "rising", "normal": 10, "peak": 90, "active_minutes": 30}
        first = world.metric_datapoints("case", "CPUUtilization", spec, now=NOW)
        second = world.metric_datapoints("case", "CPUUtilization", spec, now=NOW)
        assert [p["Value"] for p in first] == [p["Value"] for p in second]

    def test_different_cases_get_different_series(self):
        spec = {"shape": "rising", "normal": 10, "peak": 90, "active_minutes": 30}
        a = world.metric_datapoints("case_a", "CPUUtilization", spec, now=NOW)
        b = world.metric_datapoints("case_b", "CPUUtilization", spec, now=NOW)
        assert [p["Value"] for p in a] != [p["Value"] for p in b]

    def test_newest_point_is_before_now(self):
        """GetMetricStatistics treats EndTime as exclusive, so a point stamped
        at `now` is never returned and the second-newest silently becomes
        'latest'. That inverted every spike and flapping case once already."""
        spec = {"shape": "flat_normal", "normal": 50, "peak": 50, "active_minutes": 30}
        points = world.metric_datapoints("c", "CPUUtilization", spec, now=NOW)
        assert max(p["Timestamp"] for p in points) < NOW

    def test_no_data_shape_produces_nothing(self):
        spec = {"shape": "no_data", "normal": 0, "peak": 0}
        assert world.metric_datapoints("c", "M", spec, now=NOW) == []

    def test_unknown_shape_is_an_error(self):
        with pytest.raises(ValueError, match="Unknown metric shape"):
            world.metric_datapoints(
                "c", "M", {"shape": "sideways", "normal": 1, "peak": 2}, now=NOW
            )

    @pytest.mark.parametrize(
        "shape,check",
        [
            ("sustained_high", lambda v: v[-1] > 80),
            ("recovered", lambda v: v[-1] < 30),
            ("flat_normal", lambda v: max(v) < 30),
            ("rising", lambda v: v[-1] > v[0]),
            ("growing_backlog", lambda v: v[-1] > v[len(v) // 2]),
            ("draining_backlog", lambda v: v[-1] < v[0]),
            ("step_up", lambda v: v[-1] > v[0] * 2),
        ],
    )
    def test_shape_semantics(self, shape, check):
        spec = {"shape": shape, "normal": 20, "peak": 90, "active_minutes": 60}
        points = world.metric_datapoints("c", "M", spec, now=NOW)
        active = [p["Value"] for p in points][-60:]
        assert check(active), f"{shape} did not behave as its name claims"

    def test_single_spike_leaves_the_latest_value_at_baseline(self):
        """This is what makes a spike a 'do not page': the breach is over."""
        spec = {"shape": "single_spike", "normal": 20, "peak": 90, "active_minutes": 30}
        values = [p["Value"] for p in world.metric_datapoints("c", "M", spec, now=NOW)]
        assert max(values) == 90
        assert values[-1] < 30

    def test_falling_is_an_alias_of_rising(self):
        """For LessThanThreshold metrics the alarming value is the lower one."""
        spec = {"shape": "falling", "normal": 6000, "peak": 200, "active_minutes": 60}
        values = [p["Value"] for p in world.metric_datapoints("c", "M", spec, now=NOW)]
        assert values[-1] < values[0]
        assert values[-1] < 400

    def test_deploy_items_are_keyed_for_the_range_query(self):
        case = {
            "id": "c",
            "alert": {"service": "checkout-api"},
            "scenario": {"deploys": [{"minutes_ago": 20, "version": "v1"}]},
        }
        items = world.scenario_deploy_items(case, now=NOW)
        assert items[0]["PK"] == "DEPLOY#checkout-api"
        assert items[0]["SK"] == (NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert items[0]["SK"] == items[0]["deployed_at"]

    def test_runbook_items_share_one_partition(self):
        """The corpus is small and bounded, so one Query returns all of it."""
        items = world.runbook_items()
        assert {item["PK"] for item in items} == {"RUNBOOK"}
        assert len({item["SK"] for item in items}) == len(items)

    def test_every_runbook_has_page_guidance(self, cases):
        for runbook in world.RUNBOOKS:
            assert runbook["page_guidance"]
            assert runbook["steps"]
            assert runbook["severity_hint"].startswith("SEV")


class TestPromptMarker:
    def test_the_ab_marker_still_exists(self):
        """`prompt_sensitive` distinguishes the variants by this literal. If the
        heading is reworded the offline experiment reports a false null result,
        which looks exactly like 'the change had no effect'."""
        from agent.prompts import V1_BASELINE, V2_INVESTIGATE_FIRST
        from eval.fake_bedrock import V2_MARKER

        assert V2_MARKER in V2_INVESTIGATE_FIRST
        assert V2_MARKER not in V1_BASELINE

    def test_variants_differ_on_one_axis_only(self):
        """Everything except the investigation policy must be byte-identical, or
        the experiment measures incidental rewording as well as the change."""
        from agent.prompts import (
            _ENVIRONMENT,
            _GROUNDING,
            _OUTPUT_FORMAT,
            _ROLE,
            _SEVERITY,
            V1_BASELINE,
            V2_INVESTIGATE_FIRST,
        )

        for block in (_ROLE, _SEVERITY, _ENVIRONMENT, _GROUNDING, _OUTPUT_FORMAT):
            assert block.rstrip() in V1_BASELINE
            assert block.rstrip() in V2_INVESTIGATE_FIRST
