"""boto3 client construction, built once per container.

Client construction is expensive -- botocore parses a JSON service model off
disk, resolves the endpoint, and loads credentials. Doing that inside the
handler pays the cost on every invocation; doing it at module scope pays it once
per cold start and every warm invocation reuses it. That is the single largest
easy win on Lambda p50 latency and it is why these live behind module-level
caches rather than being constructed in `lambda_handler`.

The Bedrock client also gets a longer read timeout and boto3's own retries
turned down. A Converse call with tool use routinely takes 20-40 seconds, which
is well past the 60-second default only when the model is slow -- but boto3's
default `max_attempts` retry would silently re-issue a request the model is
still working on, doubling the token bill. Retries are handled deliberately in
`agent.agent._converse` instead, where they can distinguish throttling from a
malformed request.
"""

from __future__ import annotations

from typing import Any

_CLIENTS: dict[tuple[str, str], Any] = {}
_TABLES: dict[tuple[str, str], Any] = {}


def _boto3():
    import boto3  # imported lazily so the eval harness can run without it loaded

    return boto3


def bedrock_client(region: str) -> Any:
    key = ("bedrock-runtime", region)
    if key not in _CLIENTS:
        from botocore.config import Config as BotoConfig

        _CLIENTS[key] = _boto3().client(
            "bedrock-runtime",
            region_name=region,
            config=BotoConfig(
                read_timeout=120,
                connect_timeout=10,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _CLIENTS[key]


def cloudwatch_client(region: str) -> Any:
    key = ("cloudwatch", region)
    if key not in _CLIENTS:
        _CLIENTS[key] = _boto3().client("cloudwatch", region_name=region)
    return _CLIENTS[key]


def sns_client(region: str) -> Any:
    key = ("sns", region)
    if key not in _CLIENTS:
        _CLIENTS[key] = _boto3().client("sns", region_name=region)
    return _CLIENTS[key]


def dynamodb_table(region: str, table_name: str) -> Any:
    """Return the boto3 *resource* Table, not the low-level client.

    The resource layer marshals Python types to and from DynamoDB's typed
    attribute format, which removes a whole class of `{"S": ...}` bugs from the
    tool code. It costs a little clarity about what is on the wire; that trade is
    worth it here because every tool touches DynamoDB and none of them need
    wire-level control.
    """
    key = (region, table_name)
    if key not in _TABLES:
        _TABLES[key] = _boto3().resource("dynamodb", region_name=region).Table(table_name)
    return _TABLES[key]


def reset_cache() -> None:
    """Drop cached clients. Tests use this between moto contexts."""
    _CLIENTS.clear()
    _TABLES.clear()
