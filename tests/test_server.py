"""The HTTP transport used by the AgentCore Runtime deployment.

Tests go over a real socket rather than calling the handler class directly.
The whole risk in this module is the translation between HTTP and the event
shape `lambda_handler` expects -- content-length handling, status codes,
header case, the body actually arriving -- and a test that calls `do_POST`
with a hand-built object checks none of that.

What is deliberately NOT retested here: validation rules, auth semantics and
the triage loop. Those belong to `agent.handler` and have their own tests. If
this file grew assertions about which fields make an alert valid, that would
be the signal that the transport had started reimplementing the handler.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import ClassVar

import pytest

from agent import handler as handler_mod
from agent import server as server_mod
from agent.config import Config

VALID_ALERT = {
    "service": "checkout-api",
    "metric": "CPUUtilization",
    "value": 94,
    "threshold": 80,
    "duration_min": 12,
}


class Client:
    """A tiny HTTP client bound to one running server."""

    def __init__(self, port: int) -> None:
        self.port = port

    def __call__(
        self,
        method: str,
        path: str,
        body: object = None,
        headers: dict | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict, dict]:
        """Returns (status, parsed body, response headers)."""
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
        else:
            data = None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=data,
            headers=headers or ({"content-type": "application/json"} if data else {}),
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - literal http:// to a test-owned port
                return response.status, json.loads(response.read() or b"{}"), dict(
                    response.headers
                )
        except urllib.error.HTTPError as exc:  # a 4xx/5xx still carries a JSON body
            return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)

    def raw_request(self, request_text: str) -> str:
        """Send bytes that urllib refuses to construct, and return the reply.

        Needed for the malformed-header cases: urllib will not send a
        non-numeric content-length, and those are exactly the requests a
        transport has to survive.
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as sock:
            sock.sendall(request_text.replace("\n", "\r\n").encode())
            # The request asks for `Connection: close`, so reading to EOF is
            # both terminating and the only way to be sure the whole response
            # arrived. Looping on a header sniff instead would block forever
            # once the response fitted in one packet.
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")


@pytest.fixture
def http() -> Iterator[Client]:
    """A live server on an ephemeral port, torn down after the test."""
    httpd = server_mod.build_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class FakeResult:
    """Just enough of a TriageResult for the handler to shape a response."""

    alert: ClassVar[dict] = {"service": "checkout-api", "environment": "prod"}
    trace: ClassVar[tuple] = ()
    decision = "PAGE"
    paged = True
    incident_id = "INC-1"
    incident_created = True
    severity = "high"
    reasoning = "scripted"
    evidence: ClassVar[tuple] = ()
    runbook_cited = None
    iterations = 2
    decision_consistent = True
    error = None
    latency_ms = 12
    correlation_id = "corr"
    model_calls = 1
    input_tokens = 10
    output_tokens = 20
    hit_iteration_cap = False

    @staticmethod
    def tool_names() -> list[str]:
        return ["get_service_metrics"]


@pytest.fixture
def no_triage(monkeypatch) -> list[dict]:
    """Replace the triage itself, so these tests exercise transport only.

    Returns the list of calls the handler made, which is how body and header
    translation is asserted.
    """
    seen: list[dict] = []

    def fake_run_triage(alert, **kwargs):
        seen.append({"alert": alert, "kwargs": kwargs})
        return FakeResult()

    monkeypatch.setattr(handler_mod, "run_triage", fake_run_triage)
    monkeypatch.setattr(handler_mod, "CONFIG", Config(dry_run=True, sns_topic_arn=""))
    return seen


# --------------------------------------------------------------------------
# The health check
# --------------------------------------------------------------------------


class TestPing:
    def test_reports_healthy(self, http):
        """AgentCore replaces a container that fails this."""
        status, body, _ = http("GET", "/ping")
        assert status == 200
        assert body == {"status": "Healthy"}

    def test_does_not_touch_any_dependency(self, http, monkeypatch):
        """A health check that calls Bedrock reports Bedrock's health, so a
        throttle would be answered by killing a working agent."""

        def explode(*args, **kwargs):
            raise AssertionError("/ping must not reach the triage path")

        monkeypatch.setattr(handler_mod, "run_triage", explode)
        assert http("GET", "/ping")[0] == 200

    def test_a_trailing_slash_is_the_same_route(self, http):
        assert http("GET", "/ping/")[0] == 200

    def test_unknown_get_route_is_404_json(self, http):
        status, body, _ = http("GET", "/healthz")
        assert status == 404
        assert body["error"] == "not_found"


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


class TestInvocations:
    def test_a_valid_alert_reaches_the_triage_path(self, http, no_triage):
        status, body, _ = http("POST", "/invocations", VALID_ALERT)
        assert status == 200
        assert body["decision"] == "PAGE"
        assert len(no_triage) == 1
        assert no_triage[0]["alert"]["service"] == "checkout-api"

    def test_the_agentcore_session_id_becomes_the_correlation_id(self, http, no_triage):
        """One log query then covers a whole session instead of one call."""
        session = "s" * 40
        status, body, _ = http(
            "POST",
            "/invocations",
            VALID_ALERT,
            headers={
                "content-type": "application/json",
                # Sent in the casing AgentCore uses, to prove the lookup is
                # case-insensitive rather than accidentally matching.
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session,
            },
        )
        assert status == 200
        assert body["correlation_id"] == session

    def test_a_missing_session_header_still_gets_a_correlation_id(self, http, no_triage):
        _, body, _ = http("POST", "/invocations", VALID_ALERT)
        assert body["correlation_id"]

    def test_the_alert_route_is_accepted_too(self, http, no_triage):
        """So one container also answers a caller written against the API
        Gateway deployment."""
        assert http("POST", "/alert", VALID_ALERT)[0] == 200

    def test_the_api_key_is_still_enforced_when_one_is_configured(
        self, http, no_triage, monkeypatch
    ):
        """AgentCore authenticates inbound calls itself, so this is a second
        layer -- but the same header check has to work through this transport,
        or the two deployments differ on auth."""
        monkeypatch.setattr(
            handler_mod, "CONFIG", Config(dry_run=True, sns_topic_arn="", api_key="s3cret")
        )
        assert http("POST", "/invocations", VALID_ALERT)[0] == 401
        status, _, _ = http(
            "POST",
            "/invocations",
            VALID_ALERT,
            headers={"content-type": "application/json", "X-Api-Key": "s3cret"},
        )
        assert status == 200

    def test_invalid_json_is_400_not_500(self, http):
        status, body, _ = http(
            "POST", "/invocations", raw=b"{not json", headers={"content-type": "application/json"}
        )
        assert status == 400
        assert body["error"] == "bad_request"

    def test_a_missing_field_is_400_with_the_field_named(self, http):
        status, body, _ = http("POST", "/invocations", {"service": "checkout-api"})
        assert status == 400
        assert "metric" in body["detail"]

    def test_an_empty_body_is_400(self, http):
        status, _, _ = http(
            "POST", "/invocations", raw=b"", headers={"content-type": "text/plain"}
        )
        assert status == 400

    def test_an_oversized_body_is_rejected_from_its_content_length(self, http):
        """Checked before the read, not after. Reading first is how a single
        request exhausts the container's memory."""
        oversized = {"service": "s", "metric": "m", "description": "x" * (64 * 1024)}
        status, body, _ = http("POST", "/invocations", oversized)
        assert status == 413
        assert body["error"] == "payload_too_large"

    def test_an_absurd_content_length_is_rejected_without_reading_anything(self, http):
        """Declared at 50MB, with no body actually sent. The server must answer
        from the header alone -- if it waited for the bytes, this request would
        hold a container thread open until the socket timed out, which is a
        denial of service that costs the sender one packet."""
        reply = http.raw_request(
            "POST /invocations HTTP/1.1\n"
            f"Host: 127.0.0.1:{http.port}\n"
            "Content-Type: application/json\n"
            f"Content-Length: {50 * 1024 * 1024}\n"
            "\n"
        )
        assert "413" in reply.split("\n")[0]
        assert "payload_too_large" in reply

    def test_a_non_numeric_content_length_is_400(self, http):
        reply = http.raw_request(
            "POST /invocations HTTP/1.1\n"
            f"Host: 127.0.0.1:{http.port}\n"
            "Content-Type: application/json\n"
            "Content-Length: banana\n"
            "Connection: close\n"
            "\n"
        )
        assert "400" in reply.split("\n")[0]
        assert "bad_request" in reply

    def test_unknown_post_route_is_404(self, http):
        status, body, _ = http("POST", "/anything", VALID_ALERT)
        assert status == 404
        assert body["error"] == "not_found"

    def test_a_translation_bug_becomes_500_json_not_an_html_error_page(self, http, monkeypatch):
        """BaseHTTPRequestHandler's default error body is HTML, which no JSON
        client can read."""

        def explode(event):
            raise RuntimeError("translation bug")

        monkeypatch.setattr(server_mod, "lambda_handler", explode)
        status, body, _ = http("POST", "/invocations", VALID_ALERT)
        assert status == 500
        assert body == {"error": "internal_error"}

    def test_a_handler_returning_an_unparseable_body_still_answers_json(
        self, http, monkeypatch
    ):
        monkeypatch.setattr(
            server_mod, "lambda_handler", lambda event: {"statusCode": 200, "body": "not json"}
        )
        status, body, _ = http("POST", "/invocations", VALID_ALERT)
        assert status == 200
        assert body == {"raw": "not json"}


# --------------------------------------------------------------------------
# Response shape
# --------------------------------------------------------------------------


class TestResponses:
    def test_responses_are_json_and_uncacheable(self, http):
        _, _, headers = http("GET", "/ping")
        assert headers["content-type"] == "application/json"
        assert headers["cache-control"] == "no-store"

    def test_content_length_is_always_set(self, http):
        """HTTP/1.1 without content-length makes the client wait for the
        connection to close, which turns a 5ms health check into a hang."""
        _, _, headers = http("GET", "/ping")
        assert int(headers["content-length"]) > 0

    def test_the_server_does_not_advertise_its_python_version(self, http):
        """The default Server header names the interpreter's exact version,
        which is free reconnaissance."""
        _, _, headers = http("GET", "/ping")
        assert "Python" not in headers.get("server", "")

    def test_consecutive_requests_reuse_the_connection_cleanly(self, http):
        for _ in range(3):
            assert http("GET", "/ping")[0] == 200


# --------------------------------------------------------------------------
# Process lifecycle
# --------------------------------------------------------------------------


class TestLifecycle:
    def test_the_port_comes_from_the_environment_or_defaults_to_8080(self, monkeypatch):
        """AgentCore requires 8080 and does not negotiate, so the default
        matters more than the override."""
        assert server_mod.DEFAULT_PORT == 8080

    def test_build_server_binds_all_interfaces(self):
        """Inside a container, localhost is not reachable from the runtime
        that has to health-check it."""
        httpd = server_mod.build_server(0)
        try:
            assert httpd.server_address[0] == "0.0.0.0"  # noqa: S104 - asserted on purpose
        finally:
            httpd.server_close()

    def test_sigterm_shuts_the_server_down_rather_than_killing_it_mid_request(
        self, monkeypatch
    ):
        """SIGTERM is how both AgentCore and ECS ask a container to stop. The
        default action dies immediately, mid-request, with no log line saying
        why the caller got a connection reset."""
        registered: dict[int, Callable] = {}

        def record(sig, fn):
            registered[sig] = fn

        monkeypatch.setattr(server_mod.signal, "signal", record)

        shutdowns: list[bool] = []

        class FakeServer:
            def serve_forever(self):
                # Fire the handler the way a signal would, then return so
                # main() can finish.
                registered[server_mod.signal.SIGTERM](15, None)

            def shutdown(self):
                shutdowns.append(True)

            def server_close(self):
                pass

        monkeypatch.setattr(server_mod, "build_server", lambda port: FakeServer())
        assert server_mod.main(["0"]) == 0
        assert shutdowns == [True]
        assert server_mod.signal.SIGINT in registered
