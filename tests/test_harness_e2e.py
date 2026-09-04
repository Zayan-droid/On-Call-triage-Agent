"""End-to-end harness behaviour, including the proof that it discriminates.

The most important test in this file is `TestDiscrimination`. An eval that has
only ever seen a well-behaved agent has never demonstrated it can detect a
badly-behaved one -- it might be returning 1.0 for structural reasons. Running
deliberately broken agents through the whole pipeline and asserting that the
*right metric* catches each one is what makes the numbers mean anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.config import Config
from eval import backends as backend_mod
from eval import judge as judge_mod
from eval import report, run
from eval.fake_bedrock import ScriptedBedrock

REGION = "us-east-1"


def sweep(cases, policy="good", prompt_variant="v2_investigate_first", judge="heuristic"):
    """Run the real pipeline over `cases` with a scripted model."""
    cfg = Config(
        region=REGION,
        table_name="triage-harness-test",
        dry_run=True,
        prompt_variant=prompt_variant,
    )
    bedrock = ScriptedBedrock(policy=policy)
    with backend_mod.offline_backends(cases, cfg=cfg, bedrock=bedrock) as bk:
        records, scores = run.run_sweep(
            cases,
            cfg=cfg,
            bk=bk,
            judge=judge_mod.build_judge(judge),
            prompt_variant=prompt_variant,
            model_id=f"scripted:{policy}",
            miss_weight=5.0,
            false_page_weight=1.0,
            progress=False,
        )
        payload = run.build_payload(
            records,
            scores,
            meta={
                "run_id": f"test-{policy}",
                "suite": policy,
                "tag": policy,
                "mode": "offline",
                "prompt_variant": prompt_variant,
                "model_id": f"scripted:{policy}",
                "judge": judge,
            },
            miss_weight=5.0,
            false_page_weight=1.0,
        )
        return payload, bk


@pytest.fixture(scope="module")
def all_cases():
    return run.load_cases(Path(__file__).resolve().parent.parent / "eval" / "alerts.jsonl")


@pytest.fixture(scope="module")
def good_run(all_cases):
    payload, _ = sweep(all_cases)
    return payload


class TestFullSweep:
    def test_every_case_produces_a_score(self, good_run, all_cases):
        assert good_run["summary"]["overall"]["n"] == len(all_cases)
        assert len(good_run["cases"]) == len(all_cases)

    def test_no_case_errored(self, good_run):
        assert good_run["summary"]["overall"]["degraded_cases"] == 0

    def test_no_case_hit_the_iteration_cap(self, good_run):
        """A full investigation is six model calls against a cap of eight. If
        this fails, the cap is too tight and cases are being cut off mid-work."""
        assert good_run["summary"]["overall"]["iteration_cap_hits"] == 0

    def test_the_reference_agent_investigates_everything(self, good_run):
        assert good_run["summary"]["overall"]["investigated_at_all"] == 1.0
        assert good_run["summary"]["overall"]["tool_selection_exact"] == 1.0

    def test_it_misses_no_real_incident(self, good_run):
        assert good_run["summary"]["overall"]["escalation"]["recall"] == 1.0

    def test_it_stays_quiet_on_almost_all_noise(self, good_run):
        noise = good_run["summary"]["by_bucket"]["noise"]["escalation"]
        assert noise["fp"] == 0
        assert noise["tn"] == 10

    def test_decisions_match_what_the_agent_actually_did(self, good_run):
        assert good_run["summary"]["overall"]["decision_consistency"] == 1.0

    def test_every_bucket_is_scored(self, good_run):
        assert len(good_run["summary"]["by_bucket"]) == 4

    def test_the_trace_is_kept_for_audit(self, good_run):
        """A score you cannot trace back to the call that produced it is a score
        you cannot defend."""
        case = good_run["cases"][0]
        assert case["result"]["trace"]
        assert case["result"]["trace"][0]["arguments"]
        assert case["result"]["trace"][0]["result"] is not None


class TestDiscrimination:
    """Each broken agent must be caught, and by the metric that is supposed to."""

    def test_trigger_happy_is_caught_by_selection_and_escalation(self, all_cases):
        payload, _ = sweep(all_cases, policy="trigger_happy")
        overall = payload["summary"]["overall"]
        assert overall["tool_selection_exact"] == 0.0
        assert overall["investigated_at_all"] == 0.0
        assert overall["escalation"]["false_page_rate"] == 1.0
        # It does catch every real incident -- which is exactly why a single
        # accuracy number would flatter it. Recall alone hides this agent.
        assert overall["escalation"]["recall"] == 1.0

    def test_lazy_is_caught_by_recall(self, all_cases):
        payload, _ = sweep(all_cases, policy="lazy")
        overall = payload["summary"]["overall"]
        assert overall["escalation"]["recall"] == 0.0
        assert overall["escalation"]["fn"] > 0
        assert overall["tool_selection_exact"] == 0.0
        # And its false-page rate is a perfect 0.0, which is the mirror image of
        # the point above: neither number is safe on its own.
        assert overall["escalation"]["false_page_rate"] == 0.0

    def test_hallucinator_is_caught_only_by_groundedness(self, all_cases):
        """This is the case that justifies paying for metric 3. Tool selection,
        parameters and escalation all look healthy; the reasoning is fiction."""
        payload, _ = sweep(all_cases, policy="hallucinator")
        overall = payload["summary"]["overall"]
        assert overall["tool_selection_exact"] == 1.0
        assert overall["tool_parameter_accuracy"] > 0.9
        assert overall["escalation"]["recall"] == 1.0
        assert overall["groundedness"] < 0.2

    def test_bad_params_is_caught_by_parameter_accuracy(self, all_cases):
        """Right tools, wrong arguments. Metric 1 cannot see this at all."""
        payload, _ = sweep(all_cases, policy="bad_params")
        overall = payload["summary"]["overall"]
        assert overall["tool_selection_exact"] == 1.0
        assert overall["tool_parameter_accuracy"] < 0.7

    def test_phantom_incident_is_caught_by_consistency(self, all_cases):
        subset = all_cases[:5]
        payload, _ = sweep(subset, policy="phantom_incident")
        overall = payload["summary"]["overall"]
        assert overall["decision_consistency"] == 0.0
        assert overall["escalation"]["tp"] == 0  # the guardrail blocked every page

    def test_never_stops_is_caught_by_the_cap_counter(self, all_cases):
        payload, _ = sweep(all_cases[:4], policy="never_stops")
        assert payload["summary"]["overall"]["iteration_cap_hits"] == 4


class TestExperiment:
    def test_the_prompt_change_moves_the_false_page_rate(self, all_cases):
        baseline, _ = sweep(all_cases, policy="prompt_sensitive", prompt_variant="v1_baseline")
        treatment, _ = sweep(
            all_cases, policy="prompt_sensitive", prompt_variant="v2_investigate_first"
        )

        before = baseline["summary"]["overall"]["escalation"]
        after = treatment["summary"]["overall"]["escalation"]

        assert before["false_page_rate"] > after["false_page_rate"]
        # The claim that matters is that it did not buy the improvement by
        # missing real incidents.
        assert after["recall"] >= before["recall"]

    def test_the_comparison_names_the_cases_that_changed(self, all_cases):
        baseline, _ = sweep(all_cases, policy="prompt_sensitive", prompt_variant="v1_baseline")
        treatment, _ = sweep(
            all_cases, policy="prompt_sensitive", prompt_variant="v2_investigate_first"
        )
        rendered = report.render_comparison(baseline, treatment)
        assert "false-page rate" in rendered
        assert "Cases whose escalation changed" in rendered
        # A delta with no case behind it is a harness bug, not a result.
        assert "FP -> TN" in rendered


class TestPersistence:
    def test_results_write_to_dynamodb(self, all_cases):
        payload, bk = sweep(all_cases[:3])
        with backend_mod.offline_backends(all_cases[:3], cfg=bk.cfg, bedrock=None) as fresh:
            written = run.write_results_dynamo(fresh.ddb, "run-1", payload)
            assert written == 4  # one summary plus three cases

            from boto3.dynamodb.conditions import Key

            items = fresh.ddb.query(KeyConditionExpression=Key("PK").eq("RUN#run-1"))["Items"]
            assert len(items) == 4
            keys = {item["SK"] for item in items}
            assert "SUMMARY" in keys
            assert any(k.startswith("CASE#") for k in keys)

    def test_floats_survive_the_dynamodb_round_trip(self, all_cases):
        from decimal import Decimal

        payload, bk = sweep(all_cases[:2])
        with backend_mod.offline_backends(all_cases[:2], cfg=bk.cfg, bedrock=None) as fresh:
            run.write_results_dynamo(fresh.ddb, "run-2", payload)
            item = fresh.ddb.get_item(Key={"PK": "RUN#run-2", "SK": "SUMMARY"})["Item"]
            # DynamoDB rejects float outright; everything must arrive as Decimal.
            assert isinstance(item["summary"]["escalation"]["accuracy"], Decimal)

    def test_scores_emit_as_cloudwatch_metrics(self, all_cases):
        # A mixed slice: an all-positive one leaves false_page_rate undefined,
        # and an undefined metric is deliberately not published.
        mixed = [c for c in all_cases if c["bucket"] == "clear_incident"][:3] + [
            c for c in all_cases if c["bucket"] == "noise"
        ][:3]
        payload, bk = sweep(mixed)
        with backend_mod.offline_backends(mixed, cfg=bk.cfg, bedrock=None) as fresh:
            names = run.emit_cloudwatch_scores(fresh.cw, "run-3", payload)
            assert "FalsePageRate" in names
            assert "ToolSelectionAccuracy" in names
            listed = fresh.cw.list_metrics(Namespace=run.EVAL_NAMESPACE)["Metrics"]
            assert {m["MetricName"] for m in listed} >= {"FalsePageRate", "EscalationRecall"}
            dimensions = {d["Name"] for d in listed[0]["Dimensions"]}
            # Low cardinality only -- a run-id dimension would create a new
            # metric per run, which is both expensive and unalarmable.
            assert dimensions == {"PromptVariant", "Suite"}

    def test_undefined_metrics_are_omitted_not_sent_as_zero(self, all_cases):
        """Sending 0.0 for an undefined precision would trip a quality alarm on
        a run that had nothing wrong with it."""
        noise_only = [c for c in all_cases if c["bucket"] == "noise"]
        payload, bk = sweep(noise_only)
        assert payload["summary"]["overall"]["escalation"]["precision"] is None

        with backend_mod.offline_backends(noise_only, cfg=bk.cfg, bedrock=None) as fresh:
            names = run.emit_cloudwatch_scores(fresh.cw, "run-4", payload)
            assert "EscalationPrecision" not in names
            assert "FalsePageRate" in names


class TestReportRendering:
    def test_single_run_text(self, good_run):
        rendered = report.render_run(good_run)
        assert "Tool selection" in rendered
        assert "False-page rate" in rendered
        assert "TP/FP/TN/FN" in rendered
        assert "offline mode scripts the model" in rendered

    def test_markdown_is_a_table(self, good_run):
        rendered = report.render_run(good_run, markdown=True)
        assert "| Metric | Score |" in rendered
        assert "|---|---|" in rendered

    def test_undefined_values_render_as_na_never_zero(self, good_run):
        import copy

        payload = copy.deepcopy(good_run)
        payload["summary"]["overall"]["escalation"]["precision"] = None
        payload["summary"]["overall"]["groundedness"] = None
        rendered = report.render_run(payload)
        assert "n/a" in rendered
        assert "Escalation precision                    n/a" in rendered

    def test_all_buckets_appear(self, good_run):
        rendered = report.render_run(good_run)
        for bucket in ("clear_incident", "needs_investigation", "multi_step_correlation", "noise"):
            assert bucket in rendered

    def test_find_run_prefers_the_newest_match(self):
        runs = [
            {"meta": {"run_id": "1", "tag": "baseline", "suite": "s"}, "summary": {}},
            {"meta": {"run_id": "2", "tag": "baseline", "suite": "s"}, "summary": {}},
        ]
        assert report.find_run(runs, "baseline")["meta"]["run_id"] == "2"

    def test_find_run_lists_options_when_it_misses(self):
        runs = [{"meta": {"run_id": "1", "tag": "baseline", "suite": "s"}, "summary": {}}]
        with pytest.raises(SystemExit, match="baseline"):
            report.find_run(runs, "nope")

    def test_load_results_prefers_the_full_file_over_the_summary(self, tmp_path, good_run):
        full = {"meta": good_run["meta"], "summary": good_run["summary"], "cases": good_run["cases"]}
        slim = {"meta": good_run["meta"], "summary": good_run["summary"], "cases": []}
        (tmp_path / "r.json").write_text(json.dumps(full, default=str), encoding="utf-8")
        (tmp_path / "r.summary.json").write_text(json.dumps(slim, default=str), encoding="utf-8")

        loaded = report.load_results(tmp_path)
        assert len(loaded) == 1, "the same run must not be counted twice"
        assert loaded[0]["cases"]

    def test_summary_file_alone_is_still_reportable(self, tmp_path, good_run):
        slim = {
            "meta": good_run["meta"],
            "summary": good_run["summary"],
            "cases": [{"score": c["score"]} for c in good_run["cases"]],
        }
        (tmp_path / "r.summary.json").write_text(json.dumps(slim, default=str), encoding="utf-8")
        loaded = report.load_results(tmp_path)
        assert len(loaded) == 1
        assert "False-page rate" in report.render_run(loaded[0])


class TestOfflineBackends:
    def test_credentials_are_restored_afterwards(self, all_cases):
        import os

        before = os.environ.get("AWS_ACCESS_KEY_ID")
        cfg = Config(region=REGION, table_name="t", dry_run=True)
        with backend_mod.offline_backends(all_cases[:1], cfg=cfg, bedrock=None):
            pass
        assert os.environ.get("AWS_ACCESS_KEY_ID") == before

    def test_the_world_is_seeded(self, all_cases):
        from boto3.dynamodb.conditions import Key

        cfg = Config(region=REGION, table_name="t", dry_run=True)
        case = [c for c in all_cases if c["scenario"].get("deploys")][0]
        with backend_mod.offline_backends([case], cfg=cfg, bedrock=None) as bk:
            runbooks = bk.ddb.query(KeyConditionExpression=Key("PK").eq("RUNBOOK"))
            assert len(runbooks["Items"]) == len(__import__("eval.world", fromlist=["x"]).RUNBOOKS)

            deploys = bk.ddb.query(
                KeyConditionExpression=Key("PK").eq(f"DEPLOY#{case['alert']['service']}")
            )
            assert deploys["Items"]

    def test_each_context_starts_from_a_clean_table(self, all_cases):
        """Otherwise incidents from a previous run dedupe against this one and
        the sweep silently stops opening incidents."""
        from boto3.dynamodb.conditions import Key

        cfg = Config(region=REGION, table_name="t", dry_run=True)
        for _ in range(2):
            with backend_mod.offline_backends(all_cases[:1], cfg=cfg, bedrock=None) as bk:
                incidents = bk.ddb.scan(
                    FilterExpression="entity = :e",
                    ExpressionAttributeValues={":e": "incident"},
                )
                assert incidents["Items"] == []
