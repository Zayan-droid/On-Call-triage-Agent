"""Turn result JSON into tables a human reads and a README can paste.

    python -m eval.report                      # the most recent run
    python -m eval.report --list               # every run on disk
    python -m eval.report --compare baseline treatment
    python -m eval.report --markdown           # README-ready

Every number here can be undefined -- precision with no positive predictions,
groundedness with no judge. Undefined prints as `n/a`. It never prints as 0.00,
because 0.00 reads as "scored badly" and would be a lie.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).parent / "results"


def fmt(value: Any, places: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{places}f}" if isinstance(value, float) else str(value)
    return str(value)


def _delta(new: Any, old: Any, places: int = 3) -> str:
    if new is None or old is None:
        return "n/a"
    change = new - old
    sign = "+" if change > 0 else ""
    return f"{sign}{change:.{places}f}"


def load_results(directory: Path = RESULTS_DIR) -> list[dict]:
    """Load every run, oldest first.

    Each run may be on disk twice: the full trace file and the committed
    `.summary.json`. Deduplicated by run id, preferring the full file when both
    are present -- the summary is a subset, so reporting from it is correct but
    reporting the same run twice would break `--compare` and double every list.
    """
    by_run: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "meta" not in payload or "summary" not in payload:
            continue
        payload["_path"] = str(path)
        payload["_full"] = not path.name.endswith(".summary.json")
        run_id = payload["meta"].get("run_id") or path.stem
        incumbent = by_run.get(run_id)
        if incumbent is None or (payload["_full"] and not incumbent["_full"]):
            by_run[run_id] = payload
    return [by_run[k] for k in sorted(by_run)]


def find_run(runs: list[dict], key: str) -> dict:
    """Match on tag, suite, or run id -- whichever the caller had to hand.

    Searched newest-first. Tags are reused across runs by design (you re-run
    `--tag baseline` after a change), and silently reporting the first one ever
    written would show a stale number that looks exactly like a real one.
    """
    for run in reversed(runs):
        meta = run["meta"]
        if key in (meta.get("tag"), meta.get("suite"), meta.get("run_id")):
            return run
    available = ", ".join(
        sorted({r["meta"].get("tag") or r["meta"].get("suite") or r["meta"]["run_id"] for r in runs})
    )
    raise SystemExit(f"No run matching {key!r}. Available: {available or '(none)'}")


# --------------------------------------------------------------------------
# Single run
# --------------------------------------------------------------------------


def render_run(payload: dict, markdown: bool = False) -> str:
    meta = payload["meta"]
    overall = payload["summary"]["overall"]
    esc = overall["escalation"]
    lines: list[str] = []

    heading = "##" if markdown else ""
    lines.append("")
    lines.append(f"{heading} Run {meta['run_id']} - {meta.get('suite')}".strip())
    lines.append(
        f"mode={meta['mode']}  model={meta['model_id']}  prompt={meta['prompt_variant']}  "
        f"judge={meta['judge']}  cases={overall['n']}"
    )
    if meta.get("mode") == "offline":
        lines.append(
            "NOTE: offline mode scripts the model. These numbers measure the harness "
            "and the tools, not a model's judgement."
        )
    lines.append("")

    rows = [
        ("1. Tool selection (exact match)", fmt(overall["tool_selection_exact"])),
        ("   Tool selection F1", fmt(overall["tool_selection_f1"])),
        ("   Investigated at all", fmt(overall["investigated_at_all"])),
        ("2. Tool parameter accuracy", fmt(overall["tool_parameter_accuracy"])),
        ("   Parameter coverage", fmt(overall["tool_parameter_coverage"])),
        ("3. Groundedness", fmt(overall["groundedness"])),
        ("   Judge agreement", fmt(overall.get("judge_agreement"))),
        ("4. Escalation precision", fmt(esc["precision"])),
        ("   Escalation recall", fmt(esc["recall"])),
        ("   Escalation F1", fmt(esc["f1"])),
        ("   False-page rate", fmt(esc["false_page_rate"])),
        ("   Missed-page rate", fmt(esc["missed_page_rate"])),
        (
            f"   Weighted cost (miss x{fmt(esc['weights']['miss'], 0)}, "
            f"false x{fmt(esc['weights']['false_page'], 0)})",
            fmt(esc["weighted_cost"]),
        ),
        ("   Confusion TP/FP/TN/FN", f"{esc['tp']}/{esc['fp']}/{esc['tn']}/{esc['fn']}"),
        ("   Decision self-report consistency", fmt(overall["decision_consistency"])),
    ]

    if markdown:
        lines.append("| Metric | Score |")
        lines.append("|---|---|")
        lines.extend(f"| {name.strip()} | {value} |" for name, value in rows)
    else:
        lines.extend(f"  {name:<42} {value}" for name, value in rows)
    lines.append("")

    # Per bucket. The noise bucket is the one that matters and it gets its own
    # column: false-page rate is meaningless averaged over cases that should page.
    bucket_rows = [
        (
            bucket,
            stats["n"],
            fmt(stats["tool_selection_exact"], 2),
            fmt(stats["tool_parameter_accuracy"], 2),
            fmt(stats["groundedness"], 2),
            f"{stats['escalation']['tp']}/{stats['escalation']['fp']}/"
            f"{stats['escalation']['tn']}/{stats['escalation']['fn']}",
            fmt(stats["escalation"]["false_page_rate"], 2),
        )
        for bucket, stats in payload["summary"]["by_bucket"].items()
    ]
    if markdown:
        lines.append("| Bucket | n | Tool sel. | Tool params | Grounded | TP/FP/TN/FN | False-page |")
        lines.append("|---|---|---|---|---|---|---|")
        lines.extend("| " + " | ".join(str(c) for c in row) + " |" for row in bucket_rows)
    else:
        lines.append(
            f"  {'bucket':<24}{'n':>4}{'tools':>8}{'params':>8}{'ground':>8}"
            f"{'TP/FP/TN/FN':>14}{'falsepg':>9}"
        )
        for row in bucket_rows:
            lines.append(
                f"  {row[0]:<24}{row[1]:>4}{row[2]:>8}{row[3]:>8}{row[4]:>8}"
                f"{row[5]:>14}{row[6]:>9}"
            )
    lines.append("")

    failures = payload["summary"].get("failures") or []
    if failures:
        lines.append(f"{'Cases needing attention' if not markdown else '### Cases needing attention'} ({len(failures)}):")
        for failure in failures[:20]:
            bits = []
            if failure["escalation"] == "FP":
                bits.append("FALSE PAGE")
            if failure["escalation"] == "FN":
                bits.append("MISSED PAGE")
            if failure["missing_tools"]:
                bits.append(f"missing {','.join(failure['missing_tools'])}")
            if failure.get("forbidden_called"):
                bits.append(f"forbidden {','.join(failure['forbidden_called'])}")
            if failure["param_failures"]:
                bits.append(f"params: {failure['param_failures'][0]}")
            if failure["error"]:
                bits.append(f"error: {failure['error'][:80]}")
            lines.append(f"  - {failure['case_id']:<20} {'; '.join(bits)}")
        if len(failures) > 20:
            lines.append(f"  ... and {len(failures) - 20} more (see the results JSON)")
        lines.append("")
    else:
        lines.append("  No failures.\n")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Comparison -- the experiment
# --------------------------------------------------------------------------

COMPARISON_ROWS = [
    ("Tool selection (exact)", lambda o: o["tool_selection_exact"], "higher"),
    ("Tool parameter accuracy", lambda o: o["tool_parameter_accuracy"], "higher"),
    ("Groundedness", lambda o: o["groundedness"], "higher"),
    ("Escalation precision", lambda o: o["escalation"]["precision"], "higher"),
    ("Escalation recall", lambda o: o["escalation"]["recall"], "higher"),
    ("Escalation F1", lambda o: o["escalation"]["f1"], "higher"),
    ("False-page rate", lambda o: o["escalation"]["false_page_rate"], "lower"),
    ("Missed-page rate", lambda o: o["escalation"]["missed_page_rate"], "lower"),
    ("Weighted cost", lambda o: o["escalation"]["weighted_cost"], "lower"),
    ("Mean investigation calls", lambda o: o.get("mean_investigation_calls"), "-"),
    ("Total output tokens", lambda o: float(o["total_output_tokens"]), "lower"),
]


def render_comparison(baseline: dict, treatment: dict, markdown: bool = False) -> str:
    left = baseline["summary"]["overall"]
    right = treatment["summary"]["overall"]
    left_name = baseline["meta"].get("tag") or baseline["meta"]["suite"]
    right_name = treatment["meta"].get("tag") or treatment["meta"]["suite"]

    lines = ["", f"Experiment: {left_name} -> {right_name}", ""]
    lines.append(
        f"  Changed: prompt {baseline['meta']['prompt_variant']} -> "
        f"{treatment['meta']['prompt_variant']}, "
        f"model {baseline['meta']['model_id']} -> {treatment['meta']['model_id']}"
    )
    if left["n"] != right["n"]:
        lines.append(
            f"  WARNING: different case counts ({left['n']} vs {right['n']}). "
            f"These runs are not directly comparable."
        )
    lines.append("")

    rows = []
    for label, getter, direction in COMPARISON_ROWS:
        old, new = getter(left), getter(right)
        places = 0 if "token" in label.lower() else 3
        arrow = ""
        if old is not None and new is not None and direction in ("higher", "lower"):
            better = (new > old) if direction == "higher" else (new < old)
            worse = (new < old) if direction == "higher" else (new > old)
            arrow = "better" if better else ("worse" if worse else "same")
        rows.append((label, fmt(old, places), fmt(new, places), _delta(new, old, places), arrow))

    if markdown:
        lines.append(f"| Metric | {left_name} | {right_name} | Delta | |")
        lines.append("|---|---|---|---|---|")
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
    else:
        lines.append(f"  {'metric':<28}{left_name:>14}{right_name:>14}{'delta':>12}   ")
        for row in rows:
            lines.append(f"  {row[0]:<28}{row[1]:>14}{row[2]:>14}{row[3]:>12}   {row[4]}")
    lines.append("")

    # The sentence the README wants, generated rather than hand-written so it
    # cannot drift away from the numbers it claims to describe.
    old_fp = left["escalation"]["false_page_rate"]
    new_fp = right["escalation"]["false_page_rate"]
    old_recall = left["escalation"]["recall"]
    new_recall = right["escalation"]["recall"]
    if None not in (old_fp, new_fp):
        recall_clause = "no change in recall"
        if None not in (old_recall, new_recall):
            if new_recall > old_recall:
                recall_clause = f"recall up from {old_recall:.2f} to {new_recall:.2f}"
            elif new_recall < old_recall:
                recall_clause = f"recall DOWN from {old_recall:.2f} to {new_recall:.2f}"
        direction = "cut" if new_fp < old_fp else ("raised" if new_fp > old_fp else "left")
        lines.append(
            f'  Headline: "{right_name} {direction} the false-page rate from '
            f'{old_fp:.2f} to {new_fp:.2f} with {recall_clause}, over '
            f'{right["n"]} cases."'
        )
        lines.append(
            f"  Caveat to state alongside it: {right['n']} cases is a small sample. "
            f"A change of one case moves the false-page rate by "
            f"{1 / max(1, left['escalation']['fp'] + left['escalation']['tn']):.3f}."
        )
        lines.append("")

    # Which cases actually changed. This is the part that makes a delta
    # believable: a number that moved without any case changing behaviour is a
    # harness bug, not a result.
    by_id_left = {c["score"]["case_id"]: c["score"] for c in baseline["cases"]}
    by_id_right = {c["score"]["case_id"]: c["score"] for c in treatment["cases"]}
    flips = [
        (cid, by_id_left[cid]["escalation"], by_id_right[cid]["escalation"])
        for cid in sorted(by_id_left.keys() & by_id_right.keys())
        if by_id_left[cid]["escalation"] != by_id_right[cid]["escalation"]
    ]
    if flips:
        lines.append(f"  Cases whose escalation changed ({len(flips)}):")
        for cid, before, after in flips:
            lines.append(f"    {cid:<20} {before} -> {after}")
    else:
        lines.append("  No case changed its escalation outcome.")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.report")
    parser.add_argument("--dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--list", action="store_true", help="List runs on disk.")
    parser.add_argument("--run", default=None, help="Render one run by tag/suite/id.")
    parser.add_argument(
        "--compare", nargs=2, metavar=("BASELINE", "TREATMENT"), default=None
    )
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    runs = load_results(args.dir)
    if not runs:
        print(f"No results in {args.dir}. Run `python -m eval.run` first.")
        return 1

    if args.list:
        print(f"{'run_id':<20}{'suite':<28}{'mode':<9}{'n':>4}  model")
        for run in runs:
            meta = run["meta"]
            print(
                f"{meta['run_id']:<20}{str(meta.get('suite')):<28}{meta['mode']:<9}"
                f"{run['summary']['overall']['n']:>4}  {meta['model_id']}"
            )
        return 0

    if args.compare:
        print(
            render_comparison(
                find_run(runs, args.compare[0]),
                find_run(runs, args.compare[1]),
                markdown=args.markdown,
            )
        )
        return 0

    target = find_run(runs, args.run) if args.run else runs[-1]
    print(render_run(target, markdown=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
