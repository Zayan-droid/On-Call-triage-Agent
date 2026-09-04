"""The synthetic world each test case describes, and how to materialise it.

**The dataset is the single source of truth for the world.** Every case in
`alerts.jsonl` carries a `scenario` block saying what its metrics look like and
what deployed recently. This module turns that block into concrete data, and it
is used by exactly two callers:

  * `infra/seed_data.py` -- writes it to real CloudWatch and real DynamoDB
  * `eval/backends.py`   -- writes it to in-process moto backends

Both call the same functions, so an offline sweep and an against-AWS sweep
reason over identical data. That is what makes the two scores comparable; if the
offline harness invented its own world, an offline number would mean nothing
about the deployed system.

Series are generated from an explicit seed, so a case that passes today
generates byte-identical metrics tomorrow. Non-reproducible fixtures make a
regression indistinguishable from a reroll.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

# Every shape is a claim about what an operator would see on the graph. The
# names are the vocabulary the dataset uses, so adding one here is how you add a
# new kind of situation to the test set.
# In every shape, `normal` is the service's healthy baseline and `peak` is the
# alarming value. Which of the two is numerically larger depends on the metric:
# for CPU the alarming value is higher, for FreeableMemory it is lower. Shapes
# interpolate between the two and never assume a direction, which is why
# `falling` is an alias of `rising` -- the readable name in the dataset changes,
# the maths does not.
SHAPES = (
    "sustained_high",   # breaching the whole window; the real thing
    "rising",           # moving from baseline towards the alarming value
    "falling",          # same interpolation, named for metrics where low is bad
    "recovered",        # breached, then came back to baseline; already over
    "single_spike",     # one datapoint crossed; a blip
    "flapping",         # oscillating across the threshold; a bad alarm
    "flat_normal",      # nothing wrong; the alarm is lying
    "growing_backlog",  # monotonically towards the alarming value, not turning
    "draining_backlog", # peaked and coming back on its own
    "step_up",          # baseline, then a hard jump that holds (deploy-shaped)
    "no_data",          # CloudWatch has nothing at all
)


def _series(shape: str, normal: float, peak: float, points: int, rng: random.Random) -> list[float]:
    """Generate `points` values, oldest first."""
    if points <= 0:
        return []
    def jitter(v: float) -> float:
        """+/-3% noise, floored at zero. No metric here can go negative."""
        return max(0.0, v + rng.uniform(-0.03, 0.03) * max(abs(v), 1.0))

    if shape == "no_data":
        return []
    if shape == "flat_normal":
        return [jitter(normal) for _ in range(points)]
    if shape == "sustained_high":
        return [jitter(peak) for _ in range(points)]
    if shape in ("rising", "falling"):
        return [jitter(normal + (peak - normal) * (i / max(1, points - 1))) for i in range(points)]
    if shape == "recovered":
        # Breaching for the first ~60%, back to normal for the tail. The tail is
        # what makes this a "do not page" -- the problem is already over.
        breach = int(points * 0.6)
        return [jitter(peak) for _ in range(breach)] + [
            jitter(normal) for _ in range(points - breach)
        ]
    if shape == "single_spike":
        values = [jitter(normal) for _ in range(points)]
        if points >= 3:
            values[-2] = peak  # exact, not jittered: the alarm fired on this value
        else:
            values[-1] = peak
        return values
    if shape == "flapping":
        return [jitter(peak if i % 2 == 0 else normal) for i in range(points)]
    if shape == "growing_backlog":
        return [jitter(normal + (peak - normal) * ((i / max(1, points - 1)) ** 1.4)) for i in range(points)]
    if shape == "draining_backlog":
        crest = max(1, points // 4)
        head = [jitter(peak) for _ in range(crest)]
        tail = [
            jitter(peak - (peak - normal) * ((i + 1) / max(1, points - crest)))
            for i in range(points - crest)
        ]
        return head + tail
    if shape == "step_up":
        half = points // 2
        return [jitter(normal) for _ in range(half)] + [
            jitter(peak) for _ in range(points - half)
        ]
    raise ValueError(f"Unknown metric shape '{shape}'. Known shapes: {', '.join(SHAPES)}")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_datapoints(
    case_id: str,
    metric: str,
    spec: dict,
    *,
    now: datetime,
    history_minutes: int = 180,
    resolution_minutes: int = 1,
) -> list[dict]:
    """Materialise one metric series as CloudWatch-shaped datapoints.

    Three hours of minute-resolution history per metric. That is comfortably
    more than any case's `active_minutes`, so the agent can ask for a wider
    window than it strictly needs and still see baseline either side of the
    event -- which is what distinguishes "sustained" from "recovered". Seeding a
    full day instead would be ~8x the PutMetricData volume for history no case
    reaches, and CloudWatch charges per API call.

    Asking for a window longer than this history is not an error: CloudWatch
    returns only the datapoints that exist, exactly as it would in production
    for a service that started an hour ago.
    """
    shape = spec.get("shape", "flat_normal")
    if shape == "no_data":
        return []

    normal = float(spec.get("normal", 0.0))
    peak = float(spec.get("peak", normal))
    # The interesting part of the series occupies the alert's own duration; the
    # rest of the day sits at baseline. Otherwise every window looks breaching.
    active_minutes = int(spec.get("active_minutes", 30))
    unit = spec.get("unit", "None")

    total_points = max(1, history_minutes // resolution_minutes)
    active_points = min(total_points, max(1, active_minutes // resolution_minutes))
    quiet_points = total_points - active_points

    # Seed from case id + metric name: stable across runs, different per series.
    rng = random.Random(f"{case_id}:{metric}")  # noqa: S311 - reproducible fixtures, not secrets
    quiet = [
        max(0.0, normal + rng.uniform(-0.03, 0.03) * max(abs(normal), 1.0))
        for _ in range(quiet_points)
    ]
    active = _series(shape, normal, peak, active_points, rng)

    values = quiet + active
    # The newest datapoint sits one full period BEFORE `now`, not at `now`.
    #
    # GetMetricStatistics treats StartTime as inclusive and EndTime as
    # exclusive, so a datapoint stamped exactly at the query's EndTime is never
    # returned. Seeding one there made it invisible and promoted the
    # second-newest value to "latest" -- which silently inverted every
    # single_spike and flapping case, because the spike itself became the most
    # recent visible reading and the agent correctly paged on it. Four false
    # pages that were a fixture bug, not agent behaviour.
    #
    # It is also what real CloudWatch does: the period containing `now` is still
    # open and is not reported until it closes.
    start = now - timedelta(minutes=resolution_minutes * len(values))
    return [
        {
            "MetricName": metric,
            "Timestamp": start + timedelta(minutes=resolution_minutes * i),
            "Value": round(value, 3),
            "Unit": unit,
        }
        for i, value in enumerate(values)
    ]


def scenario_metric_data(case: dict, *, now: datetime, **kwargs: Any) -> list[dict]:
    """All metric datapoints for one case, ready for PutMetricData."""
    scenario = case.get("scenario") or {}
    service = case["alert"]["service"]
    out: list[dict] = []
    for metric, spec in (scenario.get("metrics") or {}).items():
        for point in metric_datapoints(case["id"], metric, spec, now=now, **kwargs):
            point["Dimensions"] = [{"Name": "Service", "Value": service}]
            out.append(point)
    return out


def scenario_deploy_items(case: dict, *, now: datetime) -> list[dict]:
    """DynamoDB items for the deploys a case says happened."""
    scenario = case.get("scenario") or {}
    service = case["alert"]["service"]
    items = []
    for index, deploy in enumerate(scenario.get("deploys") or []):
        when = now - timedelta(minutes=int(deploy["minutes_ago"]))
        timestamp = _iso(when)
        items.append(
            {
                "PK": f"DEPLOY#{service}",
                "SK": timestamp,
                "entity": "deploy",
                "deploy_id": deploy.get("deploy_id") or f"dep-{case['id']}-{index}",
                "service": service,
                "version": deploy.get("version", "unknown"),
                "deployed_at": timestamp,
                "deployed_by": deploy.get("deployed_by", "ci-pipeline"),
                "change_summary": deploy.get("change_summary", ""),
            }
        )
    return items


# --------------------------------------------------------------------------
# Runbooks -- fixed corpus, not per-case
# --------------------------------------------------------------------------

RUNBOOKS: list[dict] = [
    {
        "runbook_id": "RB-001",
        "title": "Sustained high CPU on an API service",
        "symptom": "CPU utilisation above threshold for more than five minutes on an API service",
        "keywords": ["cpu", "utilisation", "utilization", "sustained", "api", "saturation", "high"],
        "severity_hint": "SEV2",
        "page_guidance": "Page if CPU is still above threshold and not falling, and the service is user-facing in prod.",
        "steps": [
            "Confirm CPU is still breaching using get_service_metrics over at least 3x the alert duration.",
            "Check get_recent_deploys; if a release landed in the last hour, roll it back first.",
            "If no deploy correlates, scale out the service by two tasks and re-measure.",
            "Escalate to the service owner if CPU stays above threshold 10 minutes after scaling.",
        ],
    },
    {
        "runbook_id": "RB-002",
        "title": "Elevated p99 latency following a deploy",
        "symptom": "p99 latency rose sharply and a deploy landed shortly before",
        "keywords": ["latency", "p99", "slow", "deploy", "release", "regression", "elevated"],
        "severity_hint": "SEV2",
        "page_guidance": "Page when latency is still elevated and a deploy landed within the previous hour.",
        "steps": [
            "Correlate the latency step change with the deploy timestamp from get_recent_deploys.",
            "Roll back to the previous version; do not debug forward during an active regression.",
            "Confirm p99 returns to baseline within 10 minutes of the rollback.",
            "Open a follow-up ticket against the reverted change.",
        ],
    },
    {
        "runbook_id": "RB-003",
        "title": "Elevated 5xx error rate on a user-facing path",
        "symptom": "HTTP 5xx error rate above threshold on a user-facing service",
        "keywords": ["5xx", "error", "errors", "rate", "http", "failure", "user-facing", "elevated"],
        "severity_hint": "SEV1",
        "page_guidance": "Always page for sustained 5xx on checkout, payments, or auth in prod. Any user-visible error rate is a page.",
        "steps": [
            "Identify the failing dependency from the service's error logs in CloudWatch Logs Insights.",
            "Check get_recent_deploys for a correlating release and roll back if one exists.",
            "If no deploy correlates, check the health of downstream databases and caches.",
            "Consider shedding load or enabling the circuit breaker if errors exceed 10 percent.",
        ],
    },
    {
        "runbook_id": "RB-004",
        "title": "Worker queue backlog growing",
        "symptom": "queue depth or processing lag climbing steadily on a worker",
        "keywords": ["queue", "depth", "backlog", "worker", "lag", "consumer", "growing"],
        "severity_hint": "SEV3",
        "page_guidance": "Page only if the backlog is still growing. A backlog that is draining does not need a human tonight.",
        "steps": [
            "Confirm the trend with get_service_metrics -- a draining backlog needs no action.",
            "If growing, check consumer task count and whether any consumers are crash-looping.",
            "Scale consumers out; verify the queue depth turns over within 15 minutes.",
            "If the backlog is growing because of a poison message, drain to the DLQ.",
        ],
    },
    {
        "runbook_id": "RB-005",
        "title": "Database connection pool near exhaustion",
        "symptom": "database connection count approaching the configured maximum",
        "keywords": ["database", "connections", "pool", "exhaustion", "rds", "db", "connection"],
        "severity_hint": "SEV2",
        "page_guidance": "Page if connections exceed 90 percent of maximum and are still climbing.",
        "steps": [
            "Identify which service is holding connections open using Performance Insights.",
            "Check for a deploy that changed pool configuration.",
            "Terminate idle-in-transaction sessions older than 10 minutes.",
            "Raise the pool ceiling only as a temporary measure and open a ticket.",
        ],
    },
    {
        "runbook_id": "RB-006",
        "title": "Database freeable memory low",
        "symptom": "freeable memory on a database instance falling towards zero",
        "keywords": ["memory", "freeable", "ram", "database", "swap", "instance", "low"],
        "severity_hint": "SEV2",
        "page_guidance": "Page if freeable memory is below 10 percent of instance memory and falling.",
        "steps": [
            "Check for a query plan regression or a new expensive query.",
            "Confirm whether swap usage has begun; swapping on a database is a page.",
            "Kill the top memory-consuming queries if the instance is at risk.",
            "Plan an instance-class increase if this recurs.",
        ],
    },
    {
        "runbook_id": "RB-007",
        "title": "Lambda function throttling",
        "symptom": "Lambda throttles above zero, concurrency limit reached",
        "keywords": ["lambda", "throttle", "throttles", "concurrency", "limit", "function"],
        "severity_hint": "SEV3",
        "page_guidance": "Page only when throttles affect a synchronous user-facing path. Async work will retry.",
        "steps": [
            "Check whether the invocations are synchronous or event-driven.",
            "Raise reserved concurrency if the account has headroom.",
            "For async invocations, confirm the retry and DLQ configuration is intact.",
        ],
    },
    {
        "runbook_id": "RB-008",
        "title": "Known flapping alarm",
        "symptom": "an alarm that transitions between OK and ALARM repeatedly without a real problem",
        "keywords": ["flapping", "flap", "noisy", "oscillating", "intermittent", "known", "repeatedly"],
        "severity_hint": "SEV4",
        "page_guidance": "Do not page. Flapping alarms are an alarm-configuration bug, not an incident. Record it so the alarm gets fixed.",
        "steps": [
            "Confirm the metric is oscillating across the threshold rather than sustained.",
            "Do not page. Open a low-priority ticket to raise the alarm's evaluation periods.",
            "If the same alarm has flapped more than twice this week, escalate to the alarm's owner in business hours.",
        ],
    },
    {
        "runbook_id": "RB-009",
        "title": "Non-production environment alarm",
        "symptom": "an alarm fired on a dev or staging environment",
        "keywords": ["dev", "development", "staging", "stage", "non-production", "nonprod", "test", "environment"],
        "severity_hint": "SEV4",
        "page_guidance": "Do not page for non-production environments. There are no users on them. Record and leave it for business hours.",
        "steps": [
            "Confirm the environment field on the alert.",
            "Record the incident at SEV4 for the owning team to see in the morning.",
            "Do not page anyone.",
        ],
    },
    {
        "runbook_id": "RB-010",
        "title": "Traffic spike without error impact",
        "symptom": "request count well above normal while error rate and latency stay healthy",
        "keywords": ["traffic", "spike", "requests", "requestcount", "load", "volume", "surge"],
        "severity_hint": "SEV4",
        "page_guidance": "Do not page while the service is absorbing the load. Volume is not an incident; failure is.",
        "steps": [
            "Confirm error rate and latency are within normal bounds during the spike.",
            "Verify autoscaling reacted and headroom remains.",
            "Record for capacity planning. No page.",
        ],
    },
]


def runbook_items() -> list[dict]:
    """Runbook corpus as DynamoDB items."""
    return [
        {
            "PK": "RUNBOOK",
            "SK": f"SYMPTOM#{rb['runbook_id']}",
            "entity": "runbook",
            **rb,
        }
        for rb in RUNBOOKS
    ]


def all_deploy_items(cases: Iterable[dict], *, now: datetime) -> list[dict]:
    items: list[dict] = []
    for case in cases:
        items.extend(scenario_deploy_items(case, now=now))
    return items


def all_metric_data(cases: Iterable[dict], *, now: datetime, **kwargs: Any) -> list[dict]:
    data: list[dict] = []
    for case in cases:
        data.extend(scenario_metric_data(case, now=now, **kwargs))
    return data


# --------------------------------------------------------------------------
# Seeding -- identical code path for moto and for real AWS
# --------------------------------------------------------------------------

# PutMetricData accepts at most 1000 MetricDatum per request and 1MB of payload.
# 200 is a comfortable margin that still keeps the request count low.
_METRIC_BATCH = 200


def seed_dynamodb(table: Any, cases: Iterable[dict], *, now: datetime) -> dict:
    """Write runbooks and per-case deploy history."""
    cases = list(cases)
    runbooks = runbook_items()
    deploys = all_deploy_items(cases, now=now)

    # batch_writer handles the 25-item BatchWriteItem cap and retries the
    # unprocessed items DynamoDB hands back under load -- both of which are easy
    # to get wrong by hand and silently lose rows.
    with table.batch_writer() as batch:
        for item in runbooks + deploys:
            batch.put_item(Item=item)
    return {"runbooks": len(runbooks), "deploys": len(deploys)}


def seed_cloudwatch(
    cw: Any, namespace: str, cases: Iterable[dict], *, now: datetime, **kwargs: Any
) -> dict:
    """Publish every case's metric series."""
    data = all_metric_data(cases, now=now, **kwargs)
    for start in range(0, len(data), _METRIC_BATCH):
        cw.put_metric_data(
            Namespace=namespace, MetricData=data[start : start + _METRIC_BATCH]
        )
    return {"datapoints": len(data), "requests": -(-len(data) // _METRIC_BATCH)}
