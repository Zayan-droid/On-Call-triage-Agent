"""Where the harness gets its AWS clients from.

Two modes:

  **offline** (default) -- moto backends running in this process, seeded from
  `eval/world.py`. Free, deterministic, and offline in the literal sense: no
  network, no credentials, no resources to forget to delete. Crucially it is not
  a hand-rolled mock. moto evaluates real `ConditionExpression`s, so the incident
  dedupe -- the one piece of genuinely subtle DynamoDB logic here -- is tested
  against the same semantics production gets.

  **aws** -- real clients against real resources, for confirming that the
  deployed system behaves the way the offline harness says it does.

Bedrock is the exception. It has no local equivalent, so in offline mode a
scripted stand-in plays the model (see `eval/fake_bedrock.py`). That means an
offline sweep measures *the harness and the tools*, not the model's judgement.
Both numbers are useful and they are not the same number; the report labels
which one it is, and `docs/DESIGN_DECISIONS.md` D-19 spells out the difference.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any, Iterator

from agent.config import Config
from eval import world

# Credentials moto expects to exist. Set only inside the offline context so a
# real-AWS run in the same process can never pick them up.
_FAKE_CREDS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
}


class Backends:
    """The four clients a triage run needs, plus the clock it ran against."""

    def __init__(self, *, ddb: Any, cw: Any, sns: Any, bedrock: Any, now: datetime, cfg: Config):
        self.ddb = ddb
        self.cw = cw
        self.sns = sns
        self.bedrock = bedrock
        self.now = now
        self.cfg = cfg


def create_table(dynamodb: Any, table_name: str) -> Any:
    """Create the single table if it is not already there.

    One table, composite key, no secondary indexes. Every access pattern this
    system has is "give me the items under one partition key, optionally in a
    sort-key range", which a single table serves without a GSI. See
    docs/DESIGN_DECISIONS.md D-06.
    """
    existing = {t.name for t in dynamodb.tables.all()}
    if table_name in existing:
        return dynamodb.Table(table_name)
    table = dynamodb.create_table(
        TableName=table_name,
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
    table.wait_until_exists()
    return table


@contextlib.contextmanager
def offline_backends(
    cases: list[dict],
    *,
    cfg: Config,
    bedrock: Any,
    now: datetime | None = None,
    seed: bool = True,
) -> Iterator[Backends]:
    """Stand up moto backends, seed the world, yield clients, tear it all down."""
    import os

    from moto import mock_aws

    now = now or datetime.now(timezone.utc)
    saved = {k: os.environ.get(k) for k in _FAKE_CREDS}
    os.environ.update(_FAKE_CREDS)

    try:
        with mock_aws():
            import boto3

            dynamodb = boto3.resource("dynamodb", region_name=cfg.region)
            table = create_table(dynamodb, cfg.table_name)
            cw = boto3.client("cloudwatch", region_name=cfg.region)
            sns = boto3.client("sns", region_name=cfg.region)

            if seed:
                world.seed_dynamodb(table, cases, now=now)
                world.seed_cloudwatch(cw, cfg.service_metrics_namespace, cases, now=now)

            yield Backends(ddb=table, cw=cw, sns=sns, bedrock=bedrock, now=now, cfg=cfg)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def aws_backends(cfg: Config, *, now: datetime | None = None) -> Iterator[Backends]:
    """Real AWS clients. The caller is responsible for having seeded the world."""
    from agent import aws

    yield Backends(
        ddb=aws.dynamodb_table(cfg.region, cfg.table_name),
        cw=aws.cloudwatch_client(cfg.region),
        sns=aws.sns_client(cfg.region) if cfg.sns_topic_arn else None,
        bedrock=aws.bedrock_client(cfg.region),
        now=now or datetime.now(timezone.utc),
        cfg=cfg,
    )
