"""Materialise the world described by the dataset into real AWS.

    python infra/seed_data.py                      # runbooks + deploys + metrics
    python infra/seed_data.py --case clear_001     # just one case, for a demo
    python infra/seed_data.py --skip-metrics       # DynamoDB only, no PutMetricData

This calls the same `eval/world.py` functions the offline harness calls, so the
deployed system and an offline sweep reason over identical data. If this file
built its own fixtures, an offline score would say nothing about the deployed
agent, and the whole point of having both modes would be lost.

Cost note: metric seeding is the only part of this project that makes a large
number of API calls. Roughly 200 datapoints per metric per case, batched 200 to
a PutMetricData request. `--case` exists because a live demo needs one case, not
all thirty-eight.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import world  # noqa: E402
from eval.run import DEFAULT_DATASET, filter_cases, load_cases  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python infra/seed_data.py",
        description="Seed runbooks, deploy history and CloudWatch metrics.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--table", default="triage-agent")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--namespace", default="OncallTriage/Services")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--bucket", action="append", default=None)
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--skip-dynamo", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without calling AWS.",
    )
    args = parser.parse_args(argv)

    cases = filter_cases(
        load_cases(args.dataset), buckets=args.bucket, ids=args.case, limit=None
    )
    # One clock for the whole seed. Deploy timestamps and metric timestamps have
    # to agree, or "the deploy 20 minutes ago" lands beside metrics from a
    # different minute and the correlation cases stop correlating.
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    metric_data = world.all_metric_data(cases, now=now)
    deploy_items = world.all_deploy_items(cases, now=now)
    runbooks = world.runbook_items()

    print(f"Seeding {len(cases)} case(s) as of {now.isoformat()}")
    print(f"  runbooks   : {len(runbooks)}")
    print(f"  deploys    : {len(deploy_items)}")
    print(f"  datapoints : {len(metric_data)} "
          f"({-(-len(metric_data) // 200)} PutMetricData requests)")

    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0

    import boto3

    if not args.skip_dynamo:
        table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
        counts = world.seed_dynamodb(table, cases, now=now)
        print(f"\nDynamoDB {args.table}: wrote {counts['runbooks']} runbooks, "
              f"{counts['deploys']} deploys")

    if not args.skip_metrics:
        cw = boto3.client("cloudwatch", region_name=args.region)
        counts = world.seed_cloudwatch(cw, args.namespace, cases, now=now)
        print(f"CloudWatch {args.namespace}: published {counts['datapoints']} "
              f"datapoints in {counts['requests']} requests")
        # CloudWatch is eventually consistent on ingest: a metric published now
        # is typically queryable within seconds but is not guaranteed to be.
        # Worth knowing before concluding the agent is broken.
        print("\nAllow a few seconds before querying -- metric ingestion is not instant.")

    print("\nDone. Post an alert with:")
    example = cases[0]["alert"]
    print(
        f'  curl -sS -X POST "$API_URL/alert" -H "content-type: application/json" \\\n'
        f"    -d '{{\"service\":\"{example['service']}\",\"metric\":\"{example['metric']}\","
        f"\"value\":{example.get('value')},\"threshold\":{example.get('threshold')},"
        f"\"duration_min\":{example.get('duration_min')},"
        f"\"environment\":\"{example.get('environment', 'prod')}\","
        f"\"alarm_name\":\"{example.get('alarm_name')}\"}}' | jq"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
