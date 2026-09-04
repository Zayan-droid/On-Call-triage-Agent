"""Run the evaluation sweep.

    # offline, free, no AWS, no credentials -- the default
    python -m eval.run

    # the experiment: same dataset, one prompt changed
    python -m eval.run --prompt-variant v1_baseline --tag baseline
    python -m eval.run --prompt-variant v2_investigate_first --tag treatment
    python -m eval.report --compare baseline treatment

    # against the real deployed system
    python -m eval.run --mode aws --judge both --write-dynamo --emit-metrics

Every run writes a JSON file under `eval/results/`. That file is the record: it
holds the full tool trace for every case, so a score can always be traced back
to the exact call that produced it. An eval you cannot audit after the fact is
an eval you cannot trust.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.agent import run_triage
from agent.config import Config
from eval import backends as backend_mod
from eval import judge as judge_mod
from eval import scorers
from eval.fake_bedrock import POLICIES, POLICY_GOOD, ScriptedBedrock

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_DATASET = Path(__file__).parent / "alerts.jsonl"
EVAL_NAMESPACE = "OncallTriage/Eval"


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    """Read and structurally validate the dataset.

    Validated on load, loudly. A typo in a case's `expected_page` silently
    changes what "correct" means, and a harness that reports a confident wrong
    number is worse than one that refuses to start.
    """
    cases: list[dict] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} is not valid JSON: {exc.msg}") from exc

            for field in ("id", "alert", "expected_page", "expected_tools"):
                if field not in case:
                    raise ValueError(f"{path}:{lineno} case is missing '{field}'")
            if not isinstance(case["expected_page"], bool):
                raise ValueError(
                    f"{path}:{lineno} case {case['id']}: expected_page must be true or false, "
                    f"got {case['expected_page']!r}"
                )
            if case["id"] in seen:
                raise ValueError(f"{path}:{lineno} duplicate case id {case['id']!r}")
            seen.add(case["id"])

            for tool in case["expected_tools"]:
                if tool in scorers.ACTION_TOOLS:
                    raise ValueError(
                        f"{path}:{lineno} case {case['id']}: '{tool}' is an action tool "
                        f"and belongs to the escalation metric, not expected_tools."
                    )
            for tool in case.get("expected_params", {}):
                if tool not in case["expected_tools"]:
                    raise ValueError(
                        f"{path}:{lineno} case {case['id']}: expected_params names "
                        f"'{tool}', which is not in expected_tools."
                    )
            cases.append(case)

    if not cases:
        raise ValueError(f"{path} contained no cases.")
    return cases


def filter_cases(
    cases: list[dict], *, buckets: list[str] | None, ids: list[str] | None, limit: int | None
) -> list[dict]:
    if ids:
        wanted = set(ids)
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            raise ValueError(f"No such case id(s): {', '.join(sorted(missing))}")
    if buckets:
        allowed = set(buckets)
        cases = [c for c in cases if c.get("bucket") in allowed]
    if limit:
        cases = cases[:limit]
    if not cases:
        raise ValueError("Filters excluded every case.")
    return cases


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _decimalise(obj: Any) -> Any:
    """DynamoDB rejects float. Convert on the way in, via str to avoid the
    binary-float artefacts that `Decimal(0.1)` produces."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: _decimalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_decimalise(v) for v in obj]
    return obj


def write_results_dynamo(table: Any, run_id: str, payload: dict) -> int:
    """One item per case plus a run summary, under PK=RUN#<run_id>."""
    written = 0
    with table.batch_writer() as batch:
        batch.put_item(
            Item=_decimalise(
                {
                    "PK": f"RUN#{run_id}",
                    "SK": "SUMMARY",
                    "entity": "eval_run",
                    "run_id": run_id,
                    **{k: v for k, v in payload["meta"].items()},
                    "summary": payload["summary"]["overall"],
                    "by_bucket": payload["summary"]["by_bucket"],
                    "ttl": int(time.time()) + 90 * 86400,
                }
            )
        )
        written += 1
        for case in payload["cases"]:
            batch.put_item(
                Item=_decimalise(
                    {
                        "PK": f"RUN#{run_id}",
                        "SK": f"CASE#{case['score']['case_id']}",
                        "entity": "eval_case",
                        "run_id": run_id,
                        # The trace can be large; the score is what gets queried.
                        # Keeping both means a bad score is always explainable.
                        "score": case["score"],
                        "tools_called": case["result"]["tools_called"],
                        "decision": case["result"]["decision"],
                        "paged": case["result"]["paged"],
                        "reasoning": case["result"]["reasoning"][:4000],
                        "ttl": int(time.time()) + 90 * 86400,
                    }
                )
            )
            written += 1
    return written


def emit_cloudwatch_scores(cw: Any, run_id: str, payload: dict) -> list[str]:
    """Publish aggregate scores as CloudWatch custom metrics.

    This is the line that turns agent quality into an operational signal: once
    these are metrics, an alarm on tool selection accuracy or false-page rate
    fires exactly like an alarm on p99 latency. A quality regression pages you
    the same way a latency regression does.

    Dimensions are PromptVariant and Suite only -- both bounded. Adding the run
    id would create a new metric per run, which is both expensive and useless
    for alarming, since an alarm needs a time series to evaluate against.
    """
    overall = payload["summary"]["overall"]
    escalation = overall["escalation"]
    meta = payload["meta"]

    candidates = {
        "ToolSelectionAccuracy": overall["tool_selection_exact"],
        "ToolSelectionF1": overall["tool_selection_f1"],
        "ToolParameterAccuracy": overall["tool_parameter_accuracy"],
        "Groundedness": overall["groundedness"],
        "EscalationPrecision": escalation["precision"],
        "EscalationRecall": escalation["recall"],
        "EscalationF1": escalation["f1"],
        "FalsePageRate": escalation["false_page_rate"],
        "MissedPageRate": escalation["missed_page_rate"],
        "WeightedEscalationCost": escalation["weighted_cost"],
        "DecisionConsistency": overall["decision_consistency"],
        "CasesEvaluated": float(overall["n"]),
    }
    # A metric with no defined value is omitted, never sent as zero. Sending 0.0
    # for an undefined precision would trip a quality alarm on a run that had
    # nothing wrong with it.
    data = [
        {
            "MetricName": name,
            "Dimensions": [
                {"Name": "PromptVariant", "Value": meta["prompt_variant"]},
                {"Name": "Suite", "Value": meta["suite"]},
            ],
            "Value": float(value),
            "Unit": "None",
            "Timestamp": datetime.now(timezone.utc),
        }
        for name, value in candidates.items()
        if value is not None
    ]
    if data:
        cw.put_metric_data(Namespace=EVAL_NAMESPACE, MetricData=data)
    return [d["MetricName"] for d in data]


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def run_sweep(
    cases: list[dict],
    *,
    cfg: Config,
    bk: backend_mod.Backends,
    judge: Any,
    prompt_variant: str,
    model_id: str,
    miss_weight: float,
    false_page_weight: float,
    progress: bool = True,
) -> tuple[list[dict], list[scorers.CaseScore]]:
    records: list[dict] = []
    case_scores: list[scorers.CaseScore] = []

    for index, case in enumerate(cases, start=1):
        alert = dict(case["alert"])
        # Stamp every alert with the same clock the world was seeded against, so
        # "20 minutes ago" means the same thing to the data and to the agent.
        alert["timestamp"] = bk.now.strftime("%Y-%m-%dT%H:%M:%SZ")

        started = time.perf_counter()
        result = run_triage(
            alert,
            cfg=cfg,
            bedrock=bk.bedrock,
            ddb=bk.ddb,
            cw=bk.cw,
            sns=bk.sns,
            correlation_id=f"eval-{case['id']}",
            now=bk.now,
            prompt_variant=prompt_variant,
            model_id=model_id,
        )
        score = scorers.score_case(case, result)
        if judge is not None:
            score.groundedness = judge.judge(case, result)

        case_scores.append(score)
        records.append({"case": case, "result": result.to_dict(), "score": score.to_dict()})

        if progress:
            mark = {"TP": "ok", "TN": "ok", "FP": "FALSE PAGE", "FN": "MISSED PAGE"}[
                score.escalation
            ]
            grounded = (score.groundedness or {}).get("score")
            print(
                f"  [{index:>2}/{len(cases)}] {case['id']:<20} "
                f"{score.decision:<14} {mark:<11} "
                f"tools={score.tool_selection['exact_match'] and 'ok' or 'MISS'} "
                f"ground={'n/a' if grounded is None else f'{grounded:.2f}'} "
                f"{(time.perf_counter() - started) * 1000:.0f}ms",
                flush=True,
            )

    return records, case_scores


def _slim_score(score: dict) -> dict:
    """The per-case score, minus the parts only the full audit file needs.

    Drops the passing per-parameter checks (the failures are kept, and a passing
    check says nothing a reader wants) and the judge's per-case internals. Keeps
    every number the report renders.
    """
    slim = {"score": {k: v for k, v in score.items() if k != "reasoning"}}
    params = dict(slim["score"].get("tool_parameters") or {})
    params.pop("checks", None)
    slim["score"]["tool_parameters"] = params
    grounded = slim["score"].get("groundedness")
    if isinstance(grounded, dict):
        slim["score"]["groundedness"] = {
            k: grounded.get(k)
            for k in ("score", "verdict", "justification", "unsupported", "agreement")
            if grounded.get(k) is not None
        }
    return slim


def build_payload(
    records: list[dict],
    case_scores: list[scorers.CaseScore],
    *,
    meta: dict,
    miss_weight: float,
    false_page_weight: float,
) -> dict:
    summary = scorers.aggregate(
        case_scores, miss_weight=miss_weight, false_page_weight=false_page_weight
    )
    agreements = [
        (s.groundedness or {}).get("agreement")
        for s in case_scores
        if (s.groundedness or {}).get("agreement") is not None
    ]
    if agreements:
        summary["overall"]["judge_agreement"] = sum(
            1 for a in agreements if a
        ) / len(agreements)
        summary["overall"]["judge_agreement_n"] = len(agreements)
    return {"meta": meta, "summary": summary, "cases": records}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run",
        description="Replay the alert suite through the triage agent and score it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--mode",
        choices=["offline", "aws"],
        default="offline",
        help="offline uses in-process moto backends and a scripted model; "
        "aws uses real resources and real Bedrock.",
    )
    parser.add_argument(
        "--policy",
        choices=list(POLICIES),
        default=POLICY_GOOD,
        help="Which scripted agent to run in offline mode. The broken policies "
        "exist to prove the harness detects bad behaviour.",
    )
    parser.add_argument("--prompt-variant", default=None)
    parser.add_argument("--model", default=None, help="Bedrock model id (aws mode).")
    parser.add_argument(
        "--judge",
        choices=["none", "heuristic", "bedrock", "both"],
        default="heuristic",
        help="Groundedness judge. 'heuristic' is free and deterministic.",
    )
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--bucket", action="append", default=None)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tag", default=None, help="Short label for this run.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--table", default=None, help="Override the DynamoDB table name.")
    parser.add_argument("--region", default=None)
    parser.add_argument("--write-dynamo", action="store_true")
    parser.add_argument("--emit-metrics", action="store_true")
    parser.add_argument(
        "--send-pages",
        action="store_true",
        help="Actually publish to SNS. Off by default: a 38-case sweep would "
        "otherwise send you 19 emails.",
    )
    parser.add_argument("--miss-weight", type=float, default=scorers.DEFAULT_MISS_WEIGHT)
    parser.add_argument(
        "--false-page-weight", type=float, default=scorers.DEFAULT_FALSE_PAGE_WEIGHT
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show the agent's INFO logs. Off by default: 38 cases x ~8 structured "
        "log lines buries the report under its own telemetry.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # The agent logs at INFO on every tool call, which is right in production and
    # unreadable across a sweep. Respect an explicit setting; otherwise quieten.
    if not args.verbose and "TRIAGE_LOG_LEVEL" not in os.environ:
        os.environ["TRIAGE_LOG_LEVEL"] = "WARN"

    cases = filter_cases(
        load_cases(args.dataset),
        buckets=args.bucket,
        ids=args.case,
        limit=args.limit,
    )

    overrides: dict[str, Any] = {"dry_run": not args.send_pages}
    if args.table:
        overrides["table_name"] = args.table
    if args.region:
        overrides["region"] = args.region
    if args.model:
        overrides["model_id"] = args.model
    if args.judge_model:
        overrides["judge_model_id"] = args.judge_model
    cfg = Config.from_env(**overrides)

    prompt_variant = args.prompt_variant or cfg.prompt_variant
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suite = args.tag or f"{args.mode}-{prompt_variant}"

    meta = {
        "run_id": run_id,
        "suite": suite,
        "tag": args.tag,
        "mode": args.mode,
        "policy": args.policy if args.mode == "offline" else None,
        "prompt_variant": prompt_variant,
        "model_id": cfg.model_id if args.mode == "aws" else f"scripted:{args.policy}",
        "judge": args.judge,
        "judge_model_id": cfg.judge_model_id if args.judge in ("bedrock", "both") else None,
        "dataset": str(args.dataset),
        "case_count": len(cases),
        "dry_run": cfg.dry_run,
        "miss_weight": args.miss_weight,
        "false_page_weight": args.false_page_weight,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    if not args.quiet:
        print(f"\nRun {run_id}  suite={suite}")
        print(f"  mode={args.mode}  prompt={prompt_variant}  model={meta['model_id']}")
        print(f"  cases={len(cases)}  judge={args.judge}  dry_run={cfg.dry_run}\n")

    started = time.perf_counter()

    if args.mode == "offline":
        bedrock = ScriptedBedrock(policy=args.policy)
        judge_client = None
        context = backend_mod.offline_backends(cases, cfg=cfg, bedrock=bedrock)
    else:
        from agent import aws

        judge_client = aws.bedrock_client(cfg.region)
        context = backend_mod.aws_backends(cfg)

    with context as bk:
        if args.judge in ("bedrock", "both") and judge_client is None:
            # The scripted model cannot judge; an LLM judge in offline mode needs
            # real Bedrock. Say so instead of silently downgrading the judge and
            # reporting a groundedness number that was produced by something else.
            raise SystemExit(
                "--judge bedrock/both requires --mode aws (the judge needs real Bedrock)."
            )
        judge = judge_mod.build_judge(
            args.judge, client=judge_client, model_id=cfg.judge_model_id
        )

        records, case_scores = run_sweep(
            cases,
            cfg=cfg,
            bk=bk,
            judge=judge,
            prompt_variant=prompt_variant,
            model_id=cfg.model_id,
            miss_weight=args.miss_weight,
            false_page_weight=args.false_page_weight,
            progress=not args.quiet,
        )

        meta["duration_s"] = round(time.perf_counter() - started, 2)
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        payload = build_payload(
            records,
            case_scores,
            meta=meta,
            miss_weight=args.miss_weight,
            false_page_weight=args.false_page_weight,
        )

        if args.write_dynamo:
            written = write_results_dynamo(bk.ddb, run_id, payload)
            print(f"\nWrote {written} items to DynamoDB table {cfg.table_name}")
        if args.emit_metrics:
            names = emit_cloudwatch_scores(bk.cw, run_id, payload)
            print(f"Published {len(names)} metrics to {EVAL_NAMESPACE}: {', '.join(names)}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or RESULTS_DIR / f"{run_id}__{suite}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # Two files, because they have different jobs and very different sizes.
    #
    # The full payload carries every tool call and every datapoint the agent
    # saw -- roughly 800KB for 38 cases. That is the audit record: when a score
    # looks wrong, it is the only thing that can settle why. It stays on disk
    # and is git-ignored.
    #
    # The summary drops the traces and keeps the scores, at roughly 3% of the
    # size. That is what gets committed, so a score from three weeks ago is
    # still in the repo to compare against without carrying megabytes of
    # metric series in git history.
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(
            {
                "meta": payload["meta"],
                "summary": payload["summary"],
                "cases": [_slim_score(c["score"]) for c in payload["cases"]],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    if not args.quiet:
        from eval.report import render_run

        print(render_run(payload))
    print(f"\nResults written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
