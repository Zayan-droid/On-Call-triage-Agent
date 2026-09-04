"""The command-line entrypoints.

These are what actually gets typed, so the failure modes worth pinning are the
operational ones: a typo in a flag must fail before the sweep starts rather than
38 cases later, and an LLM judge must not silently downgrade to the free one and
report the resulting number as if a model had produced it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import report, run


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    directory = tmp_path / "results"
    directory.mkdir()
    monkeypatch.setattr(run, "RESULTS_DIR", directory)
    monkeypatch.setattr(report, "RESULTS_DIR", directory)
    return directory


class TestRunCli:
    def test_offline_sweep_writes_both_files(self, results_dir, capsys):
        exit_code = run.main(
            ["--limit", "4", "--tag", "cli-test", "--quiet", "--judge", "heuristic"]
        )
        assert exit_code == 0

        full = list(results_dir.glob("*__cli-test.json"))
        slim = list(results_dir.glob("*__cli-test.summary.json"))
        assert len(full) == 1 and len(slim) == 1

        payload = json.loads(full[0].read_text(encoding="utf-8"))
        assert payload["summary"]["overall"]["n"] == 4
        assert payload["meta"]["mode"] == "offline"
        assert payload["meta"]["dry_run"] is True

        # The summary drops the traces and keeps every number the report renders.
        summary = json.loads(slim[0].read_text(encoding="utf-8"))
        assert summary["summary"] == payload["summary"]
        assert "trace" not in json.dumps(summary["cases"])
        assert slim[0].stat().st_size < full[0].stat().st_size / 3

    def test_pages_are_suppressed_unless_asked_for(self, results_dir):
        """A 38-case sweep would otherwise send 19 real emails."""
        run.main(["--limit", "2", "--tag", "dry", "--quiet"])
        payload = json.loads(next(results_dir.glob("*__dry.json")).read_text(encoding="utf-8"))
        assert payload["meta"]["dry_run"] is True

    def test_bucket_filter(self, results_dir):
        run.main(["--bucket", "noise", "--tag", "noise-only", "--quiet"])
        payload = json.loads(
            next(results_dir.glob("*__noise-only.json")).read_text(encoding="utf-8")
        )
        assert set(payload["summary"]["by_bucket"]) == {"noise"}

    def test_case_filter(self, results_dir):
        run.main(["--case", "clear_001", "--case", "noise_001", "--tag", "two", "--quiet"])
        payload = json.loads(next(results_dir.glob("*__two.json")).read_text(encoding="utf-8"))
        assert {c["score"]["case_id"] for c in payload["cases"]} == {"clear_001", "noise_001"}

    def test_custom_weights_reach_the_scores(self, results_dir):
        run.main(["--limit", "6", "--tag", "weighted", "--quiet", "--miss-weight", "10"])
        payload = json.loads(
            next(results_dir.glob("*__weighted.json")).read_text(encoding="utf-8")
        )
        assert payload["summary"]["overall"]["escalation"]["weights"]["miss"] == 10.0

    def test_llm_judge_offline_is_refused_not_downgraded(self, results_dir):
        """Silently falling back to the heuristic would publish a groundedness
        number that a completely different judge produced."""
        with pytest.raises(SystemExit, match="requires --mode aws"):
            run.main(["--limit", "1", "--judge", "bedrock", "--quiet"])

    def test_unknown_policy_is_rejected_by_argparse(self, results_dir):
        with pytest.raises(SystemExit):
            run.main(["--policy", "does-not-exist"])

    def test_unknown_case_id_fails_before_the_sweep(self, results_dir):
        with pytest.raises(ValueError, match="No such case id"):
            run.main(["--case", "not_a_case", "--quiet"])

    def test_run_id_is_honoured(self, results_dir):
        run.main(["--limit", "1", "--run-id", "fixed-id", "--tag", "t", "--quiet"])
        assert (results_dir / "fixed-id__t.json").exists()

    def test_explicit_out_path(self, tmp_path, results_dir):
        out = tmp_path / "custom.json"
        run.main(["--limit", "1", "--out", str(out), "--quiet"])
        assert out.exists()
        assert out.with_suffix(".summary.json").exists()

    def test_log_level_is_quietened_but_an_explicit_setting_wins(
        self, results_dir, monkeypatch
    ):
        monkeypatch.delenv("TRIAGE_LOG_LEVEL", raising=False)
        run.main(["--limit", "1", "--quiet"])
        import os

        assert os.environ["TRIAGE_LOG_LEVEL"] == "WARN"

        monkeypatch.setenv("TRIAGE_LOG_LEVEL", "DEBUG")
        run.main(["--limit", "1", "--quiet"])
        assert os.environ["TRIAGE_LOG_LEVEL"] == "DEBUG"


class TestReportCli:
    def test_no_results_is_a_clear_message(self, results_dir, capsys):
        assert report.main([]) == 1
        assert "Run `python -m eval.run` first" in capsys.readouterr().out

    def test_list(self, results_dir, capsys):
        run.main(["--limit", "1", "--tag", "a", "--quiet"])
        run.main(["--limit", "1", "--tag", "b", "--quiet"])
        assert report.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert " a " in out or out.count("\n") >= 3
        assert "b" in out

    def test_render_latest(self, results_dir, capsys):
        run.main(["--limit", "4", "--tag", "latest-test", "--quiet"])
        assert report.main([]) == 0
        assert "False-page rate" in capsys.readouterr().out

    def test_render_named_run(self, results_dir, capsys):
        run.main(["--limit", "2", "--tag", "named", "--quiet"])
        assert report.main(["--run", "named"]) == 0
        assert "named" in capsys.readouterr().out

    def test_markdown_output(self, results_dir, capsys):
        run.main(["--limit", "2", "--tag", "md", "--quiet"])
        report.main(["--markdown"])
        assert "| Metric | Score |" in capsys.readouterr().out

    def test_compare(self, results_dir, capsys):
        run.main(
            ["--policy", "prompt_sensitive", "--prompt-variant", "v1_baseline",
             "--tag", "base", "--quiet"]
        )
        run.main(
            ["--policy", "prompt_sensitive", "--prompt-variant", "v2_investigate_first",
             "--tag", "treat", "--quiet"]
        )
        assert report.main(["--compare", "base", "treat"]) == 0
        out = capsys.readouterr().out
        assert "Experiment: base -> treat" in out
        assert "Headline:" in out

    def test_compare_warns_on_mismatched_case_counts(self, results_dir, capsys):
        """Two runs over different case counts are not comparable, and a delta
        between them would be meaningless."""
        run.main(["--limit", "5", "--tag", "small", "--quiet"])
        run.main(["--limit", "10", "--tag", "big", "--quiet"])
        report.main(["--compare", "small", "big"])
        assert "not directly comparable" in capsys.readouterr().out

    def test_compare_unknown_tag_lists_what_exists(self, results_dir):
        run.main(["--limit", "1", "--tag", "only-one", "--quiet"])
        with pytest.raises(SystemExit, match="only-one"):
            report.main(["--compare", "only-one", "missing"])


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [(None, "n/a"), (0.5, "0.500"), (1, "1"), (True, "yes"), (False, "no")],
    )
    def test_fmt(self, value, expected):
        assert report.fmt(value) == expected

    def test_zero_is_not_confused_with_undefined(self):
        """The distinction the whole report hangs on."""
        assert report.fmt(0.0) == "0.000"
        assert report.fmt(None) == "n/a"
