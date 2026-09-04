"""Lambda entrypoint: HTTP API event in, triage decision out.

Responsibilities, in order:
  1. Unwrap the event (API Gateway HTTP API v2, REST v1, or a direct invoke).
  2. Authenticate, if an API key is configured.
  3. Validate the alert -- reject nonsense here, not four tool calls later.
  4. Run the triage.
  5. Emit metrics and return a response.

Everything expensive is done at module scope so warm invocations skip it.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

from agent import aws
from agent.agent import run_triage
from agent.config import Config
from agent.obs import emf_metrics, log_error, log_info, log_warn, new_correlation_id

# ---- module scope: paid once per container, not once per request ----
CONFIG = Config.from_env()
COLD_START = True

REQUIRED_ALERT_FIELDS = ("service", "metric")
VALID_ENVIRONMENTS = {"prod", "production", "staging", "stage", "dev", "development", "test"}
MAX_BODY_BYTES = 64 * 1024


class BadRequest(ValueError):
    """The caller sent something we will not triage."""


class Unauthorized(ValueError):
    """The caller did not present a valid API key."""


# --------------------------------------------------------------------------
# Event unwrapping
# --------------------------------------------------------------------------


def _lower_headers(event: dict) -> dict:
    """HTTP header names are case-insensitive; API Gateway does not normalise
    them consistently across payload versions. Normalise once, here, rather than
    guessing at every lookup."""
    return {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}


def extract_body(event: dict) -> dict:
    """Get the alert payload out of whatever shape the event arrived in."""
    if not isinstance(event, dict):
        raise BadRequest("Event must be a JSON object.")

    # Direct invoke (console test, `aws lambda invoke`, the eval harness):
    # the alert is the event itself.
    if "body" not in event:
        if "alert" in event and isinstance(event["alert"], dict):
            return event["alert"]
        return event

    body = event.get("body")
    if body is None:
        raise BadRequest("Request body is empty.")

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as exc:
            raise BadRequest(f"Body is not valid base64: {exc}") from exc

    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")

    if isinstance(body, dict):
        return body

    if len(body) > MAX_BODY_BYTES:
        raise BadRequest(f"Request body exceeds {MAX_BODY_BYTES} bytes.")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"Body is not valid JSON: {exc.msg} at position {exc.pos}.") from exc

    if not isinstance(parsed, dict):
        raise BadRequest("Body must be a JSON object, not a list or scalar.")
    return parsed.get("alert") if isinstance(parsed.get("alert"), dict) else parsed


def check_auth(event: dict, cfg: Config) -> None:
    """Shared-secret API key check.

    A shared secret is the right amount of auth for this project and explicitly
    the wrong amount for production -- see the README's limitations section. In
    production this endpoint would sit behind an API Gateway JWT authorizer or
    be invoked over EventBridge with no public surface at all.

    Compared with `hmac.compare_digest` rather than `==` so the comparison does
    not leak the key's prefix through response timing.
    """
    if not cfg.api_key:
        return  # no key configured -> auth disabled (local and test runs)
    import hmac

    presented = _lower_headers(event).get("x-api-key") or ""
    if not hmac.compare_digest(str(presented), cfg.api_key):
        raise Unauthorized("Missing or invalid x-api-key header.")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _coerce_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BadRequest(f"Field '{field}' must be a number, got a boolean.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise BadRequest(f"Field '{field}' must be a number, got '{value}'.") from exc
    raise BadRequest(f"Field '{field}' must be a number, got {type(value).__name__}.")


def validate_alert(raw: dict) -> dict:
    """Normalise and validate. Returns a clean alert; raises BadRequest.

    Validating here rather than trusting the model to cope is deliberate. A
    missing `service` produces a confident investigation of the wrong thing,
    which is far worse than a 400.
    """
    if not isinstance(raw, dict):
        raise BadRequest("Alert must be a JSON object.")

    missing = [f for f in REQUIRED_ALERT_FIELDS if not str(raw.get(f) or "").strip()]
    if missing:
        raise BadRequest(
            f"Alert is missing required field(s): {', '.join(missing)}. "
            f"Required: {', '.join(REQUIRED_ALERT_FIELDS)}."
        )

    environment = str(raw.get("environment") or "prod").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        raise BadRequest(
            f"Unknown environment '{environment}'. "
            f"Expected one of: {', '.join(sorted(VALID_ENVIRONMENTS))}."
        )
    # Collapse synonyms so the fingerprint and the prompt see one spelling.
    environment = {
        "production": "prod",
        "stage": "staging",
        "development": "dev",
    }.get(environment, environment)

    duration = raw.get("duration_min")
    if duration is not None:
        duration = _coerce_number(duration, "duration_min")
        if duration < 0:
            raise BadRequest("Field 'duration_min' must not be negative.")

    service = str(raw["service"]).strip()
    metric = str(raw["metric"]).strip()

    alert = {
        "alert_id": str(raw.get("alert_id") or "").strip() or f"alert-{int(time.time())}",
        "alarm_name": str(raw.get("alarm_name") or f"{service}-{metric}").strip(),
        "service": service,
        "environment": environment,
        "metric": metric,
        "value": _coerce_number(raw.get("value"), "value"),
        "threshold": _coerce_number(raw.get("threshold"), "threshold"),
        "comparison": str(raw.get("comparison") or "GreaterThanThreshold").strip(),
        "duration_min": duration,
        "timestamp": str(raw.get("timestamp") or "").strip()
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if raw.get("description"):
        alert["description"] = str(raw["description"])[:1000]
    if raw.get("severity"):
        alert["severity"] = str(raw["severity"])[:32]
    return alert


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            # This endpoint returns operational data about internal services and
            # is called by alarm plumbing, never a browser. No CORS header is an
            # intentional restriction, not an oversight.
            "cache-control": "no-store",
        },
        "body": json.dumps(payload, default=str),
    }


def _emit_metrics(result: Any, cfg: Config, cold: bool) -> None:
    """Custom metrics via EMF. See agent/obs.py for why not PutMetricData.

    Dimensions are Service and Environment only. Both are bounded sets. Adding
    alert_id would create one metric per alert, which is a real way to turn a
    $0.30/month CloudWatch bill into a $300 one.
    """
    failed_tools = sum(1 for c in result.trace if not c.ok)
    emf_metrics(
        namespace=cfg.metrics_namespace,
        dimensions={
            "Service": str(result.alert.get("service", "unknown")),
            "Environment": str(result.alert.get("environment", "prod")),
        },
        metrics={
            "TriageInvocations": 1,
            "PagesSent": 1 if result.paged else 0,
            "IncidentsCreated": 1 if result.incident_created else 0,
            "ToolCalls": len(result.trace),
            "FailedToolCalls": failed_tools,
            "ModelCalls": result.model_calls,
            "InputTokens": result.input_tokens,
            "OutputTokens": result.output_tokens,
            "TriageLatencyMs": result.latency_ms,
            "ModelErrors": 1 if result.error else 0,
            "IterationCapHits": 1 if result.hit_iteration_cap else 0,
            "SelfReportMismatch": 0 if result.decision_consistent else 1,
            "ColdStarts": 1 if cold else 0,
        },
        units={
            "TriageLatencyMs": "Milliseconds",
            "InputTokens": "Count",
            "OutputTokens": "Count",
        },
        correlation_id=result.correlation_id,
        alert_id=result.alert.get("alert_id"),
        decision=result.decision,
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def lambda_handler(event: dict, context: Any = None) -> dict:
    global COLD_START
    cold, COLD_START = COLD_START, False

    # Prefer the API Gateway request id so a log line can be joined to an access
    # log; fall back to our own when invoked directly.
    correlation_id = (
        (event or {}).get("requestContext", {}).get("requestId")
        or getattr(context, "aws_request_id", None)
        or new_correlation_id()
    )

    cfg = CONFIG
    log_info(
        "request_received",
        correlation_id=correlation_id,
        cold_start=cold,
        route=(event or {}).get("rawPath") or (event or {}).get("path"),
    )

    try:
        check_auth(event or {}, cfg)
        alert = validate_alert(extract_body(event or {}))
    except Unauthorized as exc:
        log_warn("request_unauthorized", correlation_id=correlation_id, error=str(exc))
        return _response(401, {"error": "unauthorized", "correlation_id": correlation_id})
    except BadRequest as exc:
        log_warn("request_invalid", correlation_id=correlation_id, error=str(exc))
        return _response(
            400,
            {"error": "bad_request", "detail": str(exc), "correlation_id": correlation_id},
        )

    try:
        result = run_triage(
            alert,
            cfg=cfg,
            bedrock=aws.bedrock_client(cfg.region),
            ddb=aws.dynamodb_table(cfg.region, cfg.table_name),
            cw=aws.cloudwatch_client(cfg.region),
            sns=aws.sns_client(cfg.region) if cfg.sns_topic_arn else None,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        # run_triage swallows model errors itself; reaching here means something
        # structural broke (missing table, bad IAM, a bug). Log it loudly with a
        # traceback and return 500 -- never a fake "no page needed" success,
        # which would look identical to a correct quiet decision.
        import traceback

        log_error(
            "triage_unhandled_error",
            correlation_id=correlation_id,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-2000:],
        )
        emf_metrics(
            namespace=cfg.metrics_namespace,
            dimensions={
                "Service": str(alert.get("service", "unknown")),
                "Environment": str(alert.get("environment", "prod")),
            },
            metrics={"TriageInvocations": 1, "UnhandledErrors": 1},
            correlation_id=correlation_id,
        )
        return _response(
            500, {"error": "internal_error", "correlation_id": correlation_id}
        )

    _emit_metrics(result, cfg, cold)

    return _response(
        200,
        {
            "correlation_id": correlation_id,
            "alert_id": alert["alert_id"],
            "decision": result.decision,
            "paged": result.paged,
            "incident_id": result.incident_id,
            "incident_created": result.incident_created,
            "severity": result.severity,
            "reasoning": result.reasoning,
            "evidence": result.evidence,
            "runbook_cited": result.runbook_cited,
            "tools_called": result.tool_names(),
            "iterations": result.iterations,
            "decision_consistent": result.decision_consistent,
            "degraded": bool(result.error),
            "latency_ms": result.latency_ms,
        },
    )
