"""The five agent tools, their Bedrock schemas, and the dispatcher.

Contract that every tool in here obeys:

* Arguments are validated *before* any AWS call. A model that hallucinates a
  parameter gets a descriptive error back as a `toolResult` with
  `status="error"`, which Bedrock feeds to the model so it can correct itself.
  It does not raise out of the loop. A crashed Lambda teaches the model nothing
  and loses the whole investigation.
* Every call, successful or not, is appended to `ToolContext.trace`. The trace
  is the ground truth the eval harness scores against -- we measure what the
  agent *did*, never what it *says* it did.
* Tool output is deliberately small. Every byte returned is a byte of context
  the model pays for on every subsequent turn of the loop, so numbers are
  rounded and lists are capped.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from agent.config import Config
from agent.obs import log_error, log_info, log_warn

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ToolInputError(ValueError):
    """The model passed an argument we will not send to AWS."""


class ToolExecutionError(RuntimeError):
    """The tool reached AWS and AWS said no."""


# --------------------------------------------------------------------------
# Static catalog
# --------------------------------------------------------------------------

# Which CloudWatch namespace/dimension a service's metrics live under. This is
# configuration, not data: it changes when the fleet changes, not per request,
# so it lives in code where it is reviewable and testable rather than in
# DynamoDB where it would cost a lookup on every single metric query.
SERVICE_NAMESPACES: dict[str, tuple[str, str]] = {
    # service: (namespace, dimension name)
}

# Metric names the synthetic fleet actually publishes. Used only to give the
# model a useful hint when a query returns nothing -- never to reject a query,
# because a hard allowlist would break the moment a real service adds a metric.
KNOWN_METRICS: dict[str, list[str]] = {
    "checkout-api": ["CPUUtilization", "Latencyp99", "Error5xxRate", "RequestCount"],
    "payments-api": ["CPUUtilization", "Latencyp99", "Error5xxRate", "RequestCount"],
    "search-api": ["CPUUtilization", "Latencyp99", "Error5xxRate", "RequestCount"],
    "auth-service": ["CPUUtilization", "Latencyp99", "Error5xxRate", "RequestCount"],
    "orders-worker": ["QueueDepth", "CPUUtilization", "ProcessingLagSeconds"],
    "notifications-worker": ["QueueDepth", "CPUUtilization", "ProcessingLagSeconds"],
    "inventory-db": ["CPUUtilization", "FreeableMemory", "DatabaseConnections"],
    "orders-db": ["CPUUtilization", "FreeableMemory", "DatabaseConnections"],
    "image-resizer": ["CPUUtilization", "Duration", "Errors", "Throttles"],
    "report-generator": ["CPUUtilization", "Duration", "Errors", "Throttles"],
}

# Loose names the model reaches for, mapped to what CloudWatch actually stores.
METRIC_ALIASES = {
    "cpu": "CPUUtilization",
    "cpuutilization": "CPUUtilization",
    "cpu_utilization": "CPUUtilization",
    "latency": "Latencyp99",
    "p99": "Latencyp99",
    "p99latency": "Latencyp99",
    "latencyp99": "Latencyp99",
    "latency_p99": "Latencyp99",
    "errorrate": "Error5xxRate",
    "error_rate": "Error5xxRate",
    "5xx": "Error5xxRate",
    "error5xxrate": "Error5xxRate",
    "http5xx": "Error5xxRate",
    "queuedepth": "QueueDepth",
    "queue_depth": "QueueDepth",
    "memory": "FreeableMemory",
    "freeablememory": "FreeableMemory",
    "connections": "DatabaseConnections",
    "requestcount": "RequestCount",
    "requests": "RequestCount",
    "processinglagseconds": "ProcessingLagSeconds",
    "lag": "ProcessingLagSeconds",
}

VALID_SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "on", "in", "at", "to",
    "for", "and", "or", "with", "issue", "problem", "alert", "alarm",
    "service", "has", "have", "been", "that", "this", "from", "its",
}


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool invocation, recorded whether it succeeded or not."""

    name: str
    arguments: dict
    ok: bool
    result: Any = None
    error: str | None = None
    error_kind: str | None = None
    latency_ms: float = 0.0
    sequence: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "error_kind": self.error_kind,
            "latency_ms": self.latency_ms,
            "sequence": self.sequence,
        }


@dataclass
class ToolContext:
    """Everything a tool needs, injected rather than imported.

    Clients are constructor arguments so the eval harness and the test suite
    can hand in moto-backed or in-memory doubles without monkeypatching boto3.
    """

    cfg: Config
    alert: dict
    ddb: Any = None
    cw: Any = None
    sns: Any = None
    correlation_id: str = ""
    now: datetime | None = None
    trace: list[ToolCall] = field(default_factory=list)
    known_incident_ids: set[str] = field(default_factory=set)
    pages_sent: list[dict] = field(default_factory=list)
    incidents_created: list[str] = field(default_factory=list)

    def clock(self) -> datetime:
        """Injectable clock. Time-window logic is untestable without one."""
        return self.now or datetime.now(timezone.utc)

    def called(self, name: str) -> bool:
        return any(c.name == name for c in self.trace)

    def tool_names(self) -> list[str]:
        return [c.name for c in self.trace]


# --------------------------------------------------------------------------
# Bedrock tool schemas
# --------------------------------------------------------------------------

TOOL_SPECS: list[dict] = [
    {
        "toolSpec": {
            "name": "get_service_metrics",
            "description": (
                "Read real CloudWatch metric statistics for a service over a "
                "recent time window. Use this to confirm whether an alert "
                "reflects a sustained problem or a single-datapoint blip. "
                "Returns min/max/average, a trend, and the datapoints."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name exactly as it appears in the alert, e.g. 'checkout-api'.",
                        },
                        "metric": {
                            "type": "string",
                            "description": "CloudWatch metric name, e.g. 'CPUUtilization', 'Latencyp99', 'Error5xxRate', 'QueueDepth'.",
                        },
                        "window_minutes": {
                            "type": "integer",
                            "description": "How far back to look, in minutes (5-1440). Use at least 2-3x the alert duration so you can see what came before it.",
                        },
                    },
                    "required": ["service", "metric", "window_minutes"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_recent_deploys",
            "description": (
                "List deployments for a service in the recent past, newest "
                "first. Most incidents correlate with a deploy; a spike that "
                "starts minutes after a release is a very different call from "
                "one on untouched code."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name exactly as it appears in the alert.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Lookback window in hours (1-168). 24 is a sensible default.",
                        },
                    },
                    "required": ["service", "hours"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_runbook",
            "description": (
                "Search the operations runbook for a symptom. This is the only "
                "approved source of remediation steps -- cite what it returns "
                "and never invent steps of your own. Returns an empty list when "
                "nothing matches, which is itself useful information."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "symptom": {
                            "type": "string",
                            "description": "Short natural-language description of the symptom, e.g. 'elevated p99 latency after deploy'.",
                        }
                    },
                    "required": ["symptom"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "create_incident",
            "description": (
                "Open an incident record for this alert. Deduplicated: if an "
                "incident for the same alarm fingerprint was opened within the "
                "dedupe window, this returns that existing incident instead of "
                "creating a second one. Call this before paging -- you cannot "
                "page without an incident id."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "description": "SEV1 (total outage), SEV2 (major degradation), SEV3 (minor/contained), SEV4 (informational).",
                            "enum": list(VALID_SEVERITIES),
                        },
                        "summary": {
                            "type": "string",
                            "description": "One or two sentences: what is wrong, for which service, and the evidence.",
                        },
                    },
                    "required": ["severity", "summary"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "page_oncall",
            "description": (
                "Wake up the on-call engineer. This is the highest-cost action "
                "available to you: it interrupts a human being, possibly at "
                "3am. Only call it when the evidence you have gathered shows a "
                "real, active, user-affecting problem that cannot wait for "
                "business hours. Requires an incident id from create_incident."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "The incident id returned by create_incident.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this needs a human now, citing the specific evidence you gathered.",
                        },
                    },
                    "required": ["incident_id", "reason"],
                }
            },
        }
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(s["toolSpec"]["name"] for s in TOOL_SPECS)


def tool_config() -> dict:
    """The `toolConfig` argument for bedrock-runtime Converse."""
    return {"tools": [dict(spec) for spec in TOOL_SPECS]}


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _require_str(args: dict, key: str, *, max_len: int = 2000) -> str:
    value = args.get(key)
    if value is None:
        raise ToolInputError(f"Missing required argument '{key}'.")
    if not isinstance(value, str):
        raise ToolInputError(
            f"Argument '{key}' must be a string, got {type(value).__name__}."
        )
    value = value.strip()
    if not value:
        raise ToolInputError(f"Argument '{key}' must not be empty.")
    if len(value) > max_len:
        raise ToolInputError(f"Argument '{key}' is too long (max {max_len} characters).")
    return value


def _require_int(args: dict, key: str, low: int, high: int) -> int:
    value = args.get(key)
    if value is None:
        raise ToolInputError(f"Missing required argument '{key}'.")
    # Models routinely send "30" instead of 30. Coerce rather than fail: this is
    # a formatting slip, not a reasoning error, and failing it would pollute the
    # tool-parameter-accuracy metric with noise that is not about the agent.
    if isinstance(value, bool):
        raise ToolInputError(f"Argument '{key}' must be an integer, got a boolean.")
    if isinstance(value, str):
        try:
            value = int(float(value.strip()))
        except ValueError:
            raise ToolInputError(f"Argument '{key}' must be an integer, got '{value}'.")
    elif isinstance(value, float):
        if value != int(value):
            raise ToolInputError(f"Argument '{key}' must be a whole number, got {value}.")
        value = int(value)
    elif not isinstance(value, int):
        raise ToolInputError(
            f"Argument '{key}' must be an integer, got {type(value).__name__}."
        )
    if value < low or value > high:
        raise ToolInputError(
            f"Argument '{key}' must be between {low} and {high}, got {value}."
        )
    return value


def _unknown_args(args: dict, allowed: set[str]) -> list[str]:
    return sorted(k for k in args if k not in allowed)


def normalise_metric(metric: str) -> str:
    """Map a loose metric name onto the one CloudWatch stores."""
    key = re.sub(r"[\s\-_%]+", "", metric.strip().lower())
    return METRIC_ALIASES.get(key, metric.strip())


def _round(value: Any, places: int = 2) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, (int, float)):
        return round(float(value), places)
    return value


def _to_native(obj: Any) -> Any:
    """DynamoDB hands back Decimal; JSON and the model both want plain numbers."""
    if isinstance(obj, Decimal):
        as_float = float(obj)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_native(v) for v in obj]
    return obj


_WIRE_TYPES = {"S", "N", "B", "SS", "NS", "BS", "M", "L", "NULL", "BOOL"}


def _looks_like_wire_format(item: dict) -> bool:
    """Is this a raw AttributeValue map rather than a deserialised item?"""
    if not item:
        return False
    return all(
        isinstance(v, dict) and len(v) == 1 and next(iter(v)) in _WIRE_TYPES
        for v in item.values()
    )


def deserialise_item(item: Any) -> dict:
    """Normalise a DynamoDB item to plain Python whichever layer produced it.

    The boto3 *resource* layer deserialises successful responses, but it does not
    touch exceptions -- so the item returned by
    `ReturnValuesOnConditionCheckFailure` arrives as raw wire format
    (`{"incident_id": {"S": "INC-..."}}`) even when the write went through
    `Table.put_item`. Reading it as if it were a normal item yields a dict where
    a string is expected, and the dedupe silently returns an unusable incident
    id. Verified against both real DynamoDB semantics and moto.
    """
    if not isinstance(item, dict) or not item:
        return {}
    if _looks_like_wire_format(item):
        from boto3.dynamodb.types import TypeDeserializer

        deserialiser = TypeDeserializer()
        return {k: _to_native(deserialiser.deserialize(v)) for k, v in item.items()}
    return _to_native(item)


# --------------------------------------------------------------------------
# Fingerprinting and ids
# --------------------------------------------------------------------------


def alert_fingerprint(alert: dict) -> str:
    """Stable identity for 'the same alarm firing again'.

    Deliberately excludes `value` and `duration_min`. An alert storm is the same
    alarm re-firing with a slightly different reading each time; if the reading
    were part of the fingerprint, every re-fire would look like a new incident
    and the dedupe would never trigger -- which is exactly the bug the dedupe
    exists to prevent.
    """
    parts = [
        str(alert.get("service", "")).strip().lower(),
        str(alert.get("environment", "prod")).strip().lower(),
        str(alert.get("alarm_name", "")).strip().lower(),
        str(alert.get("metric", "")).strip().lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def new_incident_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"INC-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Tool 1: get_service_metrics
# --------------------------------------------------------------------------


def _period_for(window_minutes: int) -> int:
    """Pick a CloudWatch period that gives useful resolution without flooding.

    A 12-minute alert needs minute resolution to show whether it was sustained.
    A 24-hour window at minute resolution is 1440 datapoints, which is both slow
    and far more context than the model can use.
    """
    if window_minutes <= 90:
        return 60
    if window_minutes <= 360:
        return 300
    return 900


def _trend(values: list[float]) -> str:
    if len(values) < 4:
        return "insufficient_data"
    third = max(1, len(values) // 3)
    head = sum(values[:third]) / third
    tail = sum(values[-third:]) / third
    if head == 0:
        return "rising" if tail > 0 else "flat"
    delta = (tail - head) / abs(head)
    if delta > 0.15:
        return "rising"
    if delta < -0.15:
        return "falling"
    return "flat"


def get_service_metrics(ctx: ToolContext, args: dict) -> dict:
    service = _require_str(args, "service", max_len=128)
    metric_raw = _require_str(args, "metric", max_len=128)
    window = _require_int(
        args, "window_minutes", ctx.cfg.min_window_minutes, ctx.cfg.max_window_minutes
    )
    metric = normalise_metric(metric_raw)

    namespace, dim_name = SERVICE_NAMESPACES.get(
        service, (ctx.cfg.service_metrics_namespace, "Service")
    )
    end = ctx.clock()
    start = end - timedelta(minutes=window)
    period = _period_for(window)

    if ctx.cw is None:
        raise ToolExecutionError("CloudWatch client is not configured.")

    response = ctx.cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=[{"Name": dim_name, "Value": service}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum", "Minimum"],
    )

    # CloudWatch does NOT guarantee datapoint ordering. Sorting is not optional;
    # an unsorted series makes "latest" and "trend" silently wrong.
    points = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])

    truncated = False
    if len(points) > ctx.cfg.max_datapoints_returned:
        # Keep the most recent window -- the tail is what the decision hinges on.
        points = points[-ctx.cfg.max_datapoints_returned :]
        truncated = True

    averages = [float(p["Average"]) for p in points if p.get("Average") is not None]
    maxima = [float(p["Maximum"]) for p in points if p.get("Maximum") is not None]
    minima = [float(p["Minimum"]) for p in points if p.get("Minimum") is not None]

    result: dict[str, Any] = {
        "service": service,
        "metric": metric,
        "namespace": namespace,
        "window_minutes": window,
        "period_seconds": period,
        "start": _iso(start),
        "end": _iso(end),
        "datapoint_count": len(points),
        "truncated": truncated,
        "datapoints": [
            {
                "t": _iso(p["Timestamp"]),
                "avg": _round(p.get("Average")),
                "max": _round(p.get("Maximum")),
                "min": _round(p.get("Minimum")),
            }
            for p in points
        ],
    }

    if metric != metric_raw:
        result["normalised_from"] = metric_raw

    if averages:
        result["summary"] = {
            "avg": _round(sum(averages) / len(averages)),
            "max": _round(max(maxima) if maxima else max(averages)),
            "min": _round(min(minima) if minima else min(averages)),
            "latest": _round(averages[-1]),
            "first": _round(averages[0]),
            "trend": _trend(averages),
        }
    else:
        # No data is a real and common answer -- a dev-environment alarm on a
        # service that publishes nothing, or a metric name that does not exist.
        # Say so explicitly rather than returning an empty structure the model
        # will read as "everything is fine".
        result["summary"] = None
        result["note"] = (
            f"No datapoints for metric '{metric}' on service '{service}' in namespace "
            f"'{namespace}' over the last {window} minutes. This means CloudWatch has "
            f"no data, not that the value is zero. Do not infer a healthy service "
            f"from this."
        )
        if service in KNOWN_METRICS:
            result["known_metrics_for_service"] = KNOWN_METRICS[service]

    return result


# --------------------------------------------------------------------------
# Tool 2: get_recent_deploys
# --------------------------------------------------------------------------


def get_recent_deploys(ctx: ToolContext, args: dict) -> dict:
    service = _require_str(args, "service", max_len=128)
    hours = _require_int(args, "hours", 1, ctx.cfg.max_deploy_lookback_hours)

    now = ctx.clock()
    cutoff = now - timedelta(hours=hours)

    if ctx.ddb is None:
        raise ToolExecutionError("DynamoDB table resource is not configured.")

    from boto3.dynamodb.conditions import Key

    # Query, never Scan. The partition key is the service, so this reads only
    # that service's deploys, and the sort key range pushes the time filter into
    # DynamoDB instead of paying to read-then-discard in Lambda.
    response = ctx.ddb.query(
        KeyConditionExpression=Key("PK").eq(f"DEPLOY#{service}")
        & Key("SK").between(_iso(cutoff), _iso(now)),
        ScanIndexForward=False,  # newest first
        Limit=25,
    )

    deploys = []
    for item in response.get("Items", []):
        item = _to_native(item)
        deployed_at = item.get("deployed_at") or item.get("SK", "")
        minutes_ago = None
        try:
            dt = datetime.strptime(deployed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            minutes_ago = int((now - dt).total_seconds() // 60)
        except (ValueError, TypeError):
            pass
        deploys.append(
            {
                "deploy_id": item.get("deploy_id"),
                "service": service,
                "version": item.get("version"),
                "deployed_at": deployed_at,
                "minutes_ago": minutes_ago,
                "change_summary": item.get("change_summary"),
                "deployed_by": item.get("deployed_by"),
            }
        )

    result: dict[str, Any] = {
        "service": service,
        "lookback_hours": hours,
        "as_of": _iso(now),
        "deploy_count": len(deploys),
        "deploys": deploys,
    }
    if not deploys:
        result["note"] = (
            f"No deploys recorded for '{service}' in the last {hours} hours. "
            f"This alert is not explained by a recent release."
        )
    return result


# --------------------------------------------------------------------------
# Tool 3: search_runbook
# --------------------------------------------------------------------------


def _tokenise(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if t not in _STOPWORDS and len(t) > 2
    }


def search_runbook(ctx: ToolContext, args: dict) -> dict:
    symptom = _require_str(args, "symptom", max_len=500)

    if ctx.ddb is None:
        raise ToolExecutionError("DynamoDB table resource is not configured.")

    from boto3.dynamodb.conditions import Key

    # The runbook corpus is small and bounded (tens of entries), so one Query on
    # a single partition returns all of it for one RCU and the ranking happens in
    # Python where it is deterministic and unit-testable. A keyword GSI or a
    # vector store would be real infrastructure to maintain for no measurable
    # gain at this size. See docs/DESIGN_DECISIONS.md D-08.
    response = ctx.ddb.query(KeyConditionExpression=Key("PK").eq("RUNBOOK"))
    query_tokens = _tokenise(symptom)

    scored = []
    for item in response.get("Items", []):
        item = _to_native(item)
        keywords = {str(k).lower() for k in (item.get("keywords") or [])}
        title_tokens = _tokenise(item.get("title", ""))
        symptom_tokens = _tokenise(item.get("symptom", ""))

        # Keyword hits are worth more than incidental title-word overlap.
        keyword_hits = query_tokens & keywords
        text_hits = query_tokens & (title_tokens | symptom_tokens)
        raw = 2.0 * len(keyword_hits) + 1.0 * len(text_hits - keyword_hits)
        if raw <= 0:
            continue
        denom = max(1, len(query_tokens))
        scored.append(
            (
                round(min(1.0, raw / (2.0 * denom)), 3),
                sorted(keyword_hits | text_hits),
                item,
            )
        )

    scored.sort(key=lambda row: row[0], reverse=True)
    top = scored[:3]

    matches = [
        {
            "runbook_id": item.get("runbook_id")
            or str(item.get("SK", "")).replace("SYMPTOM#", ""),
            "title": item.get("title"),
            "symptom": item.get("symptom"),
            "match_score": score,
            "matched_terms": terms,
            "severity_hint": item.get("severity_hint"),
            "page_guidance": item.get("page_guidance"),
            "steps": item.get("steps", []),
        }
        for score, terms, item in top
    ]

    result: dict[str, Any] = {
        "query": symptom,
        "match_count": len(matches),
        "matches": matches,
    }
    if not matches:
        result["note"] = (
            "No runbook entry matched this symptom. Do not invent remediation "
            "steps. Decide based on the metric and deploy evidence alone, and "
            "say in your reasoning that no runbook covered this."
        )
    return result


# --------------------------------------------------------------------------
# Tool 4: create_incident (conditional write / dedupe)
# --------------------------------------------------------------------------


def create_incident(ctx: ToolContext, args: dict) -> dict:
    severity = _require_str(args, "severity", max_len=16).upper()
    if severity not in VALID_SEVERITIES:
        raise ToolInputError(
            f"Invalid severity '{severity}'. Must be one of {', '.join(VALID_SEVERITIES)}."
        )
    summary = _require_str(args, "summary", max_len=1000)

    if ctx.ddb is None:
        raise ToolExecutionError("DynamoDB table resource is not configured.")

    now = ctx.clock()
    now_epoch = int(now.timestamp())
    cutoff_epoch = now_epoch - ctx.cfg.dedupe_window_min * 60
    fingerprint = alert_fingerprint(ctx.alert)
    incident_id = new_incident_id(now)

    item = {
        "PK": f"INCIDENT#{fingerprint}",
        "SK": "ACTIVE",
        "entity": "incident",
        "incident_id": incident_id,
        "fingerprint": fingerprint,
        "severity": severity,
        "summary": summary,
        "service": ctx.alert.get("service"),
        "environment": ctx.alert.get("environment", "prod"),
        "alarm_name": ctx.alert.get("alarm_name"),
        "metric": ctx.alert.get("metric"),
        "alert_id": ctx.alert.get("alert_id"),
        "opened_at": _iso(now),
        "opened_at_epoch": now_epoch,
        "correlation_id": ctx.correlation_id,
        "ttl": now_epoch + ctx.cfg.incident_ttl_days * 86400,
    }

    from botocore.exceptions import ClientError

    try:
        # The whole dedupe is this one condition. Write the incident only if no
        # incident exists for this fingerprint, OR the one that exists is older
        # than the window. It is a genuine sliding window -- fixed time buckets
        # would let an alert at 14:59 and one at 15:01 both "win" and page twice.
        #
        # This is a conditional write rather than a transaction because there is
        # exactly one item whose consistency matters. TransactWriteItems would
        # cost 2x the write units and add a second failure mode to guard against
        # a race that a single-item condition already closes atomically.
        ctx.ddb.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) OR opened_at_epoch < :cutoff",
            ExpressionAttributeValues={":cutoff": cutoff_epoch},
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

        # Lost the race, or a duplicate alert. Recover the incumbent incident.
        # DynamoDB returns it in the error when ReturnValuesOnConditionCheckFailure
        # is honoured; we fall back to a GetItem when it is not (older API
        # behaviour, and some local doubles), so the tool never returns nothing.
        existing = deserialise_item(exc.response.get("Item"))
        if not existing:
            fetched = ctx.ddb.get_item(
                Key={"PK": f"INCIDENT#{fingerprint}", "SK": "ACTIVE"}
            )
            existing = deserialise_item(fetched.get("Item"))

        existing_id = existing.get("incident_id")
        log_info(
            "incident_deduplicated",
            correlation_id=ctx.correlation_id,
            fingerprint=fingerprint,
            existing_incident_id=existing_id,
        )
        if existing_id:
            ctx.known_incident_ids.add(existing_id)
        return {
            "incident_id": existing_id,
            "created": False,
            "deduplicated": True,
            "fingerprint": fingerprint,
            "existing_severity": existing.get("severity"),
            "existing_summary": existing.get("summary"),
            "opened_at": existing.get("opened_at"),
            "note": (
                f"An incident for this exact alarm was already opened at "
                f"{existing.get('opened_at')} (within the {ctx.cfg.dedupe_window_min} "
                f"minute dedupe window). No new incident was created; "
                f"{existing_id} is the live one. Consider whether paging again "
                f"adds anything -- the on-call may already have been notified."
            ),
        }

    # Immutable audit copy, keyed by incident id so incident history survives the
    # ACTIVE pointer being overwritten by the next occurrence. Best-effort by
    # design: the conditional item above is the source of truth, and failing the
    # whole triage because an audit row did not land would be the wrong trade.
    try:
        audit = dict(item)
        audit["PK"] = f"INCIDENT#{incident_id}"
        audit["SK"] = "META"
        audit["entity"] = "incident_audit"
        ctx.ddb.put_item(Item=audit)
    except Exception as exc:  # pragma: no cover - best effort by design
        log_warn(
            "incident_audit_write_failed",
            correlation_id=ctx.correlation_id,
            incident_id=incident_id,
            error=str(exc)[:300],
        )

    ctx.known_incident_ids.add(incident_id)
    ctx.incidents_created.append(incident_id)
    log_info(
        "incident_created",
        correlation_id=ctx.correlation_id,
        incident_id=incident_id,
        severity=severity,
        fingerprint=fingerprint,
    )
    return {
        "incident_id": incident_id,
        "created": True,
        "deduplicated": False,
        "fingerprint": fingerprint,
        "severity": severity,
        "opened_at": _iso(now),
    }


# --------------------------------------------------------------------------
# Tool 5: page_oncall
# --------------------------------------------------------------------------


def page_oncall(ctx: ToolContext, args: dict) -> dict:
    incident_id = _require_str(args, "incident_id", max_len=64)
    reason = _require_str(args, "reason", max_len=2000)

    # Guardrail: you cannot page against an incident that does not exist in this
    # investigation. Without it a model can hallucinate an incident id and page
    # a human about a record nobody can look up. Returned as a recoverable tool
    # error so the model can call create_incident and try again.
    if incident_id not in ctx.known_incident_ids:
        raise ToolInputError(
            f"Unknown incident_id '{incident_id}'. You must call create_incident "
            f"first and page against the id it returns. Known ids this "
            f"investigation: {sorted(ctx.known_incident_ids) or 'none'}."
        )

    env = str(ctx.alert.get("environment", "prod")).upper()
    subject = f"[{env}] Page: {ctx.alert.get('service')} ({incident_id})"
    body = json.dumps(
        {
            "incident_id": incident_id,
            "service": ctx.alert.get("service"),
            "environment": ctx.alert.get("environment", "prod"),
            "alarm_name": ctx.alert.get("alarm_name"),
            "metric": ctx.alert.get("metric"),
            "value": ctx.alert.get("value"),
            "threshold": ctx.alert.get("threshold"),
            "reason": reason,
            "correlation_id": ctx.correlation_id,
            "paged_at": _iso(ctx.clock()),
        },
        indent=2,
        default=str,
    )

    record = {"incident_id": incident_id, "reason": reason, "subject": subject}

    if ctx.cfg.dry_run or not ctx.cfg.sns_topic_arn:
        # The eval harness sweeps 38 alerts. Without this it would send 38 real
        # pages every run, which is both absurd and the exact alert fatigue the
        # project exists to measure.
        record["dry_run"] = True
        ctx.pages_sent.append(record)
        log_info(
            "page_suppressed_dry_run",
            correlation_id=ctx.correlation_id,
            incident_id=incident_id,
        )
        return {
            "paged": True,
            "dry_run": True,
            "incident_id": incident_id,
            "note": "DRY RUN: the page was recorded but no notification was sent.",
        }

    if ctx.sns is None:
        raise ToolExecutionError("SNS client is not configured.")

    response = ctx.sns.publish(
        TopicArn=ctx.cfg.sns_topic_arn,
        Subject=subject[:100],  # SNS hard-caps Subject at 100 characters.
        Message=body,
        MessageAttributes={
            "severity": {
                "DataType": "String",
                "StringValue": str(ctx.alert.get("severity", "unknown")),
            },
            "service": {
                "DataType": "String",
                "StringValue": str(ctx.alert.get("service", "unknown")),
            },
        },
    )
    record["message_id"] = response.get("MessageId")
    ctx.pages_sent.append(record)
    log_info(
        "page_sent",
        correlation_id=ctx.correlation_id,
        incident_id=incident_id,
        message_id=response.get("MessageId"),
    )
    return {
        "paged": True,
        "dry_run": False,
        "incident_id": incident_id,
        "message_id": response.get("MessageId"),
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

_HANDLERS = {
    "get_service_metrics": (get_service_metrics, {"service", "metric", "window_minutes"}),
    "get_recent_deploys": (get_recent_deploys, {"service", "hours"}),
    "search_runbook": (search_runbook, {"symptom"}),
    "create_incident": (create_incident, {"severity", "summary"}),
    "page_oncall": (page_oncall, {"incident_id", "reason"}),
}

_AWS_ERROR_TYPES = {
    "ClientError",
    "EndpointConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ToolExecutionError",
}


def dispatch(ctx: ToolContext, name: str, arguments: Any) -> tuple[dict, bool]:
    """Run one tool. Returns `(payload, is_error)`; never raises.

    Not raising is the point. Every failure mode -- unknown tool, bad argument,
    AWS refusing the call -- comes back as a structured message the model can
    read and act on, and lands in the trace so the eval can see it happened.
    """
    started = time.perf_counter()
    sequence = len(ctx.trace) + 1

    if not isinstance(arguments, dict):
        arguments = {"_raw": arguments}

    entry = _HANDLERS.get(name)
    if entry is None:
        message = f"Unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}."
        ctx.trace.append(
            ToolCall(
                name=name,
                arguments=arguments,
                ok=False,
                error=message,
                error_kind="unknown_tool",
                sequence=sequence,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
        log_warn("tool_unknown", correlation_id=ctx.correlation_id, tool=name)
        return {"error": message, "error_kind": "unknown_tool"}, True

    handler, allowed = entry
    extra = _unknown_args(arguments, allowed)
    code = None

    try:
        if extra:
            raise ToolInputError(
                f"Unexpected argument(s) {extra} for '{name}'. "
                f"Accepted arguments: {sorted(allowed)}."
            )
        result = handler(ctx, arguments)
    except ToolInputError as exc:
        payload = {"error": str(exc), "error_kind": "invalid_arguments"}
        ok, kind = False, "invalid_arguments"
        log_warn(
            "tool_invalid_arguments",
            correlation_id=ctx.correlation_id,
            tool=name,
            arguments=arguments,
            error=str(exc)[:400],
        )
    except Exception as exc:
        # Distinguish "AWS said no" from "our code is broken" in the logs; both
        # are surfaced to the model identically because it can do nothing
        # different about them.
        kind = (
            "aws_error" if type(exc).__name__ in _AWS_ERROR_TYPES else "internal_error"
        )
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
        payload = {
            "error": f"{name} failed: {type(exc).__name__}: {str(exc)[:400]}",
            "error_kind": kind,
        }
        if code:
            payload["aws_error_code"] = code
        ok = False
        log_error(
            "tool_failed",
            correlation_id=ctx.correlation_id,
            tool=name,
            error_kind=kind,
            aws_error_code=code,
            error=str(exc)[:500],
        )
    else:
        payload, ok, kind = result, True, None

    latency = round((time.perf_counter() - started) * 1000, 2)
    ctx.trace.append(
        ToolCall(
            name=name,
            arguments=arguments,
            ok=ok,
            result=payload if ok else None,
            error=None if ok else payload.get("error"),
            error_kind=kind,
            latency_ms=latency,
            sequence=sequence,
        )
    )
    if ok:
        log_info(
            "tool_ok",
            correlation_id=ctx.correlation_id,
            tool=name,
            duration_ms=latency,
            arguments=arguments,
        )
    return payload, not ok
