"""Structured logging and CloudWatch custom metrics.

Two decisions live in this file and both are worth defending:

1. Logs are single-line JSON on stdout. Lambda ships stdout to CloudWatch Logs
   for free, and single-line JSON is what makes CloudWatch Logs Insights able to
   query fields directly (`fields @timestamp, decision | filter paged = 1`).
   Multi-line pretty JSON breaks Insights because each line becomes an event.

2. Custom metrics from the request path use CloudWatch Embedded Metric Format
   (EMF) rather than PutMetricData. PutMetricData is a synchronous network call
   inside the request; it adds latency, can throttle, and can fail in a way that
   would either break triage or need swallowing. EMF is just a specially shaped
   log line -- CloudWatch extracts the metrics asynchronously. Zero added
   latency, zero new failure mode, and it costs nothing beyond the log line we
   were writing anyway.

   The eval harness is the exception: it calls PutMetricData directly, because
   it runs as a batch job off the hot path and wants the metrics to land at a
   timestamp it chooses.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}


def _min_level() -> int:
    return _LEVELS.get(os.environ.get("TRIAGE_LOG_LEVEL", "INFO").upper(), 20)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def _default(obj: Any) -> str:
    """json.dumps fallback. Decimal from DynamoDB and datetime both land here."""
    try:
        from decimal import Decimal

        if isinstance(obj, Decimal):
            return str(obj)
    except Exception:  # pragma: no cover - defensive
        pass
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return repr(obj)


def _write(payload: dict) -> None:
    # Never let observability take down the request. A logging failure that
    # kills triage would be a self-inflicted outage.
    try:
        sys.stdout.write(json.dumps(payload, default=_default) + "\n")
        sys.stdout.flush()
    except Exception:  # pragma: no cover - defensive
        try:
            sys.stdout.write('{"level":"ERROR","event":"log_serialisation_failed"}\n')
        except Exception:
            pass


def log_event(level: str, event: str, **fields: Any) -> None:
    """Emit one structured log line."""
    if _LEVELS.get(level.upper(), 20) < _min_level():
        return
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level.upper(),
        "event": event,
    }
    payload.update(fields)
    _write(payload)


def log_info(event: str, **fields: Any) -> None:
    log_event("INFO", event, **fields)


def log_warn(event: str, **fields: Any) -> None:
    log_event("WARN", event, **fields)


def log_error(event: str, **fields: Any) -> None:
    log_event("ERROR", event, **fields)


def log_debug(event: str, **fields: Any) -> None:
    log_event("DEBUG", event, **fields)


def emf_metrics(
    namespace: str,
    metrics: dict[str, float],
    dimensions: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    **fields: Any,
) -> dict:
    """Emit a CloudWatch EMF log line and return it (returned for testability).

    `dimensions` become CloudWatch dimensions, so keep cardinality low --
    every distinct dimension combination is a billed custom metric. Service name
    and environment are bounded sets; alert_id and correlation_id are NOT, so
    they are passed as plain fields (queryable in Logs Insights, not billed as
    metrics).
    """
    dimensions = dimensions or {}
    units = units or {}
    payload: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimensions.keys())] if dimensions else [[]],
                    "Metrics": [
                        {"Name": name, "Unit": units.get(name, "None")}
                        for name in metrics
                    ],
                }
            ],
        }
    }
    payload.update(dimensions)
    payload.update(metrics)
    payload.update(fields)
    payload.setdefault("event", "metrics")
    _write(payload)
    return payload


@contextmanager
def timed(event: str, **fields: Any) -> Iterator[dict]:
    """Time a block and log the duration. Logs on failure too, with the error."""
    start = time.perf_counter()
    holder: dict[str, Any] = {}
    try:
        yield holder
    except Exception as exc:
        holder["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        log_error(
            event,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            **fields,
            **holder,
        )
        raise
    else:
        holder["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        log_info(event, ok=True, **fields, **holder)
