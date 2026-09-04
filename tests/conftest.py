"""Shared fixtures.

Every fixture that touches AWS goes through moto rather than a hand-written
mock. The difference matters most for DynamoDB: the incident dedupe is a
`ConditionExpression`, and a dict-backed fake would have to reimplement
condition evaluation in order to test it -- at which point the test is checking
the fake, not the code.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

# Set before boto3 is imported anywhere, so no real credential lookup happens
# and a stray un-mocked call fails loudly instead of hitting a real account.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRIAGE_LOG_LEVEL", "ERROR")

from agent.config import Config
from agent.tools import ToolContext

TABLE = "triage-agent-test"
REGION = "us-east-1"
NAMESPACE = "OncallTriage/Services"


@pytest.fixture
def now() -> datetime:
    """A fixed clock. Every time-window assertion depends on one."""
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg() -> Config:
    return Config(
        region=REGION,
        table_name=TABLE,
        sns_topic_arn="",
        dry_run=True,
        dedupe_window_min=15,
        service_metrics_namespace=NAMESPACE,
    )


@pytest.fixture
def aws_stack():
    """moto backends plus the table. Torn down after each test."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {
            "table": dynamodb.Table(TABLE),
            "cw": boto3.client("cloudwatch", region_name=REGION),
            "sns": boto3.client("sns", region_name=REGION),
        }


@pytest.fixture
def alert() -> dict:
    return {
        "alert_id": "test-001",
        "alarm_name": "checkout-api-cpu-high",
        "service": "checkout-api",
        "environment": "prod",
        "metric": "CPUUtilization",
        "value": 94.0,
        "threshold": 80.0,
        "comparison": "GreaterThanThreshold",
        "duration_min": 12,
        "timestamp": "2026-09-04T12:00:00Z",
    }


@pytest.fixture
def ctx(cfg, alert, aws_stack, now) -> ToolContext:
    return ToolContext(
        cfg=cfg,
        alert=alert,
        ddb=aws_stack["table"],
        cw=aws_stack["cw"],
        sns=aws_stack["sns"],
        correlation_id="test-correlation",
        now=now,
    )


@pytest.fixture
def seeded_metrics(aws_stack, now):
    """Publish a simple, known CPU series so assertions can be exact.

    Deliberately hand-built rather than generated: this fixture is what the
    world generator itself is checked against, so it must not share code with it.
    """

    def _seed(service: str, metric: str, values: list[float], unit: str = "Percent"):
        # Newest point one minute before `now`: GetMetricStatistics treats
        # EndTime as exclusive, so a point stamped at `now` is invisible.
        data = [
            {
                "MetricName": metric,
                "Dimensions": [{"Name": "Service", "Value": service}],
                "Timestamp": now - timedelta(minutes=len(values) - index),
                "Value": float(value),
                "Unit": unit,
            }
            for index, value in enumerate(values)
        ]
        aws_stack["cw"].put_metric_data(Namespace=NAMESPACE, MetricData=data)
        return data

    return _seed


@pytest.fixture
def seeded_deploys(aws_stack, now):
    def _seed(service: str, minutes_ago_list: list[int]):
        items = []
        for index, minutes in enumerate(minutes_ago_list):
            stamp = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            item = {
                "PK": f"DEPLOY#{service}",
                "SK": stamp,
                "entity": "deploy",
                "deploy_id": f"dep-{index}",
                "service": service,
                "version": f"v1.{index}.0",
                "deployed_at": stamp,
                "deployed_by": "ci",
                "change_summary": f"change {index}",
            }
            aws_stack["table"].put_item(Item=item)
            items.append(item)
        return items

    return _seed


@pytest.fixture
def seeded_runbooks(aws_stack):
    from eval.world import runbook_items

    with aws_stack["table"].batch_writer() as batch:
        for item in runbook_items():
            batch.put_item(Item=item)
    return runbook_items()


@pytest.fixture
def cases():
    from pathlib import Path

    from eval.run import load_cases

    return load_cases(Path(__file__).resolve().parent.parent / "eval" / "alerts.jsonl")
