"""HTTP transport for the triage agent, for Bedrock AgentCore Runtime.

AgentCore Runtime does not invoke a Python function the way Lambda does -- it
runs a container and speaks HTTP to it. The contract is two endpoints on port
8080:

    POST /invocations   the request payload; JSON in, JSON out
    GET  /ping          liveness, which must answer {"status": "Healthy"}

The important design point is what this module does *not* contain: no auth, no
validation, no triage, no metrics. It translates an HTTP request into the same
event shape `agent.handler.lambda_handler` already accepts and translates the
response back. Both deployment targets therefore run byte-identical business
logic, and a behavioural difference between them can only be a transport bug.
Reimplementing validation here is how the two paths would quietly drift until
the eval numbers stopped describing the deployed agent.

Two decisions worth defending:

* **Standard library, not FastAPI.** Two endpoints, one of which returns a
  constant. Adding FastAPI and uvicorn would add ~30MB and two dependency
  trees to an image whose entire point is a small attack surface, to save
  about forty lines. `ThreadingHTTPServer` is enough because AgentCore routes
  one session to one container instance -- concurrency here is a health check
  arriving during an invocation, not a thousand simultaneous requests.

* **Errors become HTTP status codes, never a 200 with an error inside.**
  A caller must be able to distinguish "triage ran and decided not to page"
  from "triage failed", and a 200 body containing `{"error": ...}` makes those
  two look identical to anything reading status codes.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent.handler import MAX_BODY_BYTES, lambda_handler
from agent.obs import log_error, log_info

# AgentCore requires 8080 and does not negotiate. Overridable only so a test
# can bind an ephemeral port.
DEFAULT_PORT = 8080

# AgentCore stamps every request with the session it belongs to. Adopting it as
# the correlation id is what lets one log query cover a whole conversation
# rather than a single invocation.
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"

# An oversized body up to this size is read and discarded so the sender can
# finish writing and actually receive the 413. Anything larger is dropped.
DRAIN_LIMIT = 1024 * 1024


class TriageHTTPHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so keep-alive works; AgentCore reuses the connection across the
    # ping and the invocation.
    protocol_version = "HTTP/1.1"
    server_version = "oncall-triage/1.0"
    sys_version = ""

    # ---- plumbing -------------------------------------------------------

    def _send(self, status: int, payload: dict, *, close: bool = False) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        if close:
            # Set when the request body was NOT read -- a rejected oversized
            # payload, or an unparseable content-length. Under HTTP/1.1 the
            # unread bytes stay in the socket and the next response is read
            # as their continuation, so the client hangs waiting for a reply
            # that already arrived. Closing is the only correct answer once
            # the request has been left half-consumed.
            self.send_header("connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence BaseHTTPRequestHandler's stderr access log.

        Everything this project logs is single-line JSON on stdout, parsed by
        CloudWatch Logs Insights. An apache-style line on stderr would be the
        one log entry that no query can read.
        """

    def _headers_as_dict(self) -> dict:
        return {str(k).lower(): v for k, v in self.headers.items()}

    # ---- routes ---------------------------------------------------------

    # Method names are fixed by BaseHTTPRequestHandler's dispatch, not chosen.
    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/ping", ""):
            # Deliberately does not touch Bedrock, DynamoDB or the network.
            # A health check that calls a dependency reports that dependency's
            # health, and AgentCore replaces a container that fails it -- so a
            # Bedrock throttle would be answered by killing a working agent.
            self._send(200, {"status": "Healthy"})
            return
        self._send(404, {"error": "not_found", "detail": f"No route GET {self.path}"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/invocations", "/alert"):
            self._send(404, {"error": "not_found", "detail": f"No route POST {self.path}"})
            return

        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._send(
                400,
                {"error": "bad_request", "detail": "content-length is not a number."},
                close=True,
            )
            return

        # Bounded from the declared length, before the read -- an unbounded
        # read on a caller-supplied length is how one request exhausts a
        # container's memory. The ceiling is the same constant the Lambda path
        # enforces, so both transports reject exactly the same requests.
        if length > MAX_BODY_BYTES:
            detail = {
                "error": "payload_too_large",
                "detail": f"Body exceeds {MAX_BODY_BYTES} bytes.",
            }
            if length <= DRAIN_LIMIT:
                # Discard the body in fixed-size chunks rather than answering
                # immediately. A server that replies mid-upload and closes
                # leaves the client writing into a dead socket, and the client
                # then reports a connection reset instead of the 413 that was
                # actually sent -- a status code nobody receives is not a
                # rejection, it is an outage. Chunked so the discard itself
                # cannot be the allocation that kills the container.
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self._send(413, detail)
            else:
                # Past this size the request is abusive rather than merely
                # oversized, and draining it is doing the sender's work for
                # them. Drop the connection.
                self._send(413, detail, close=True)
            return

        raw = self.rfile.read(length) if length > 0 else b""

        headers = self._headers_as_dict()
        event = {
            "body": raw.decode("utf-8", errors="replace"),
            "headers": headers,
            "rawPath": self.path,
            "requestContext": {"requestId": headers.get(SESSION_HEADER, "")},
        }

        try:
            response = lambda_handler(event)
        except Exception as exc:
            # lambda_handler catches its own failures and returns a 500, so
            # arriving here means the translation above is broken. Report it as
            # a 500 rather than letting BaseHTTPRequestHandler answer with an
            # HTML error page that no JSON client can read.
            log_error("server_unhandled_error", error=f"{type(exc).__name__}: {exc}")
            self._send(500, {"error": "internal_error"})
            return

        status = int(response.get("statusCode", 200))
        body = response.get("body") or "{}"
        try:
            payload = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            payload = {"raw": body}
        self._send(status, payload)


def build_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    # Bind on all interfaces: inside a container, localhost is not reachable
    # from the runtime that has to health-check it.
    return ThreadingHTTPServer(("0.0.0.0", port), TriageHTTPHandler)  # noqa: S104


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else int(os.environ.get("PORT") or DEFAULT_PORT)

    httpd = build_server(port)

    def shutdown(signum: int, _frame: Any) -> None:
        # SIGTERM is how AgentCore and ECS both ask a container to stop. The
        # default action is to die immediately, mid-request, with no log line
        # explaining why the caller got a connection reset.
        log_info("server_stopping", signal=signum)
        httpd.shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)

    log_info("server_started", port=port, routes=["POST /invocations", "GET /ping"])
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        log_info("server_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
