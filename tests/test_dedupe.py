"""create_incident and the conditional-write dedupe.

This is the one place where getting DynamoDB semantics slightly wrong produces a
bug you would never see in a happy-path demo and would absolutely see during an
alert storm at 3am. It is tested against moto rather than a mock precisely
because moto evaluates the real `ConditionExpression`.

The boundary cases are the point: an alert one second inside the window must
dedupe, one second outside must not, and two racing writers must produce exactly
one incident between them.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from agent.tools import ToolContext, alert_fingerprint, dispatch


def _open_incident(ctx, *, severity="SEV2", summary="cpu is high"):
    payload, is_error = dispatch(
        ctx, "create_incident", {"severity": severity, "summary": summary}
    )
    assert not is_error, payload
    return payload


def _fresh_ctx(base: ToolContext, *, now=None, alert=None) -> ToolContext:
    """A new investigation over the same backends -- a second alert arriving."""
    return ToolContext(
        cfg=base.cfg,
        alert=alert if alert is not None else base.alert,
        ddb=base.ddb,
        cw=base.cw,
        sns=base.sns,
        correlation_id="second-investigation",
        now=now or base.now,
    )


class TestFirstWrite:
    def test_creates_and_returns_an_id(self, ctx):
        result = _open_incident(ctx)
        assert result["created"] is True
        assert result["deduplicated"] is False
        assert result["incident_id"].startswith("INC-")
        assert result["fingerprint"] == alert_fingerprint(ctx.alert)

    def test_writes_the_active_pointer_and_an_audit_copy(self, ctx):
        result = _open_incident(ctx)
        fingerprint = result["fingerprint"]
        incident_id = result["incident_id"]

        active = ctx.ddb.get_item(
            Key={"PK": f"INCIDENT#{fingerprint}", "SK": "ACTIVE"}
        )["Item"]
        audit = ctx.ddb.get_item(
            Key={"PK": f"INCIDENT#{incident_id}", "SK": "META"}
        )["Item"]

        assert active["incident_id"] == incident_id
        assert audit["incident_id"] == incident_id
        assert audit["entity"] == "incident_audit"

    def test_sets_a_ttl(self, ctx):
        result = _open_incident(ctx)
        item = ctx.ddb.get_item(
            Key={"PK": f"INCIDENT#{result['fingerprint']}", "SK": "ACTIVE"}
        )["Item"]
        assert int(item["ttl"]) > int(item["opened_at_epoch"])

    def test_page_is_authorised_by_the_returned_id(self, ctx):
        result = _open_incident(ctx)
        assert result["incident_id"] in ctx.known_incident_ids

    @pytest.mark.parametrize("severity", ["sev1", "SEV1", " sev2 "])
    def test_severity_is_case_insensitive(self, ctx, severity):
        payload, is_error = dispatch(
            ctx, "create_incident", {"severity": severity, "summary": "x"}
        )
        assert not is_error
        assert payload["severity"] == severity.strip().upper()

    def test_invalid_severity_is_rejected(self, ctx):
        payload, is_error = dispatch(
            ctx, "create_incident", {"severity": "CRITICAL", "summary": "x"}
        )
        assert is_error
        assert "SEV1" in payload["error"]


class TestDedupeWindow:
    def test_second_alert_inside_the_window_is_deduplicated(self, ctx, now):
        first = _open_incident(ctx)

        later = _fresh_ctx(ctx, now=now + timedelta(minutes=14, seconds=59))
        second = _open_incident(later, summary="cpu is still high")

        assert second["created"] is False
        assert second["deduplicated"] is True
        assert second["incident_id"] == first["incident_id"]
        assert "already opened" in second["note"]
        # The deduped id is still page-able: the incident is real, just not new.
        assert first["incident_id"] in later.known_incident_ids

    def test_alert_outside_the_window_opens_a_new_incident(self, ctx, now):
        first = _open_incident(ctx)

        later = _fresh_ctx(ctx, now=now + timedelta(minutes=15, seconds=1))
        second = _open_incident(later)

        assert second["created"] is True
        assert second["incident_id"] != first["incident_id"]

    def test_the_boundary_is_a_sliding_window_not_a_bucket(self, ctx, now):
        """Three alerts, each 10 minutes apart, inside a 15-minute window.

        With fixed time buckets the third would land in a new bucket and open a
        second incident even though only 20 minutes have passed and the previous
        incident is 10 minutes old. A sliding window keeps deduping as long as
        the alerts keep coming.
        """
        first = _open_incident(ctx)
        second = _open_incident(_fresh_ctx(ctx, now=now + timedelta(minutes=10)))
        third = _open_incident(_fresh_ctx(ctx, now=now + timedelta(minutes=20)))

        assert second["deduplicated"] is True
        assert second["incident_id"] == first["incident_id"]
        # 20 minutes after the ORIGINAL open, which is outside the window --
        # the window runs from when the incident was opened, not from the last
        # alert, so this one correctly opens a new incident.
        assert third["created"] is True

    def test_a_different_alarm_is_never_deduplicated(self, ctx):
        first = _open_incident(ctx)
        other = _fresh_ctx(ctx, alert=dict(ctx.alert, alarm_name="checkout-api-5xx"))
        second = _open_incident(other)
        assert second["created"] is True
        assert second["incident_id"] != first["incident_id"]

    def test_storm_of_identical_alerts_opens_exactly_one_incident(self, ctx, now):
        """Twelve firings of the same alarm over ten minutes."""
        results = [
            _open_incident(
                _fresh_ctx(ctx, now=now + timedelta(seconds=firing * 50)),
                summary=f"cpu at {90 + firing} percent",
            )
            for firing in range(12)
        ]

        ids = {r["incident_id"] for r in results}
        assert len(ids) == 1, "an alert storm must not open twelve incidents"
        assert sum(1 for r in results if r["created"]) == 1
        assert sum(1 for r in results if r["deduplicated"]) == 11

    def test_window_length_is_configurable(self, ctx, now):
        ctx.cfg = dataclasses.replace(ctx.cfg, dedupe_window_min=1)
        first = _open_incident(ctx)
        later = _open_incident(_fresh_ctx(ctx, now=now + timedelta(minutes=2)))
        assert later["created"] is True
        assert later["incident_id"] != first["incident_id"]


class TestDedupeRecovery:
    def test_existing_incident_is_recovered_from_the_condition_failure(self, ctx, now):
        """The incumbent must come back with a usable id.

        DynamoDB returns it inside the ClientError in raw wire format, which is
        the failure this test pins: reading it without deserialising yields
        `{'S': 'INC-...'}` where a string is expected, and the dedupe silently
        hands back an unusable incident id.
        """
        first = _open_incident(ctx, severity="SEV1", summary="original summary")
        second = _open_incident(_fresh_ctx(ctx, now=now + timedelta(minutes=5)))

        assert isinstance(second["incident_id"], str)
        assert second["incident_id"] == first["incident_id"]
        assert second["existing_severity"] == "SEV1"
        assert second["existing_summary"] == "original summary"
        assert second["opened_at"] == first["opened_at"]

    def test_falls_back_to_get_item_when_the_error_carries_no_item(
        self, ctx, now, monkeypatch
    ):
        """Older DynamoDB behaviour, and some doubles, omit the returned item."""
        first = _open_incident(ctx)

        real_put = ctx.ddb.put_item

        def strip_item(**kwargs):
            from botocore.exceptions import ClientError

            try:
                return real_put(**kwargs)
            except ClientError as exc:
                exc.response.pop("Item", None)
                raise

        later = _fresh_ctx(ctx, now=now + timedelta(minutes=5))
        monkeypatch.setattr(later.ddb, "put_item", strip_item)
        second = _open_incident(later)

        assert second["deduplicated"] is True
        assert second["incident_id"] == first["incident_id"]

    def test_a_non_condition_error_is_not_swallowed(self, ctx, monkeypatch):
        """An AccessDenied on the write must surface, not look like a dedupe."""
        from botocore.exceptions import ClientError

        def denied(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutItem"
            )

        monkeypatch.setattr(ctx.ddb, "put_item", denied)
        payload, is_error = dispatch(
            ctx, "create_incident", {"severity": "SEV2", "summary": "x"}
        )
        assert is_error
        assert payload["error_kind"] == "aws_error"
        assert payload["aws_error_code"] == "AccessDeniedException"

    def test_audit_write_failure_does_not_lose_the_incident(self, ctx, monkeypatch):
        """The audit copy is best-effort; the conditional item is the truth."""
        real_put = ctx.ddb.put_item
        calls = {"n": 0}

        def fail_second_write(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("audit table unavailable")
            return real_put(**kwargs)

        monkeypatch.setattr(ctx.ddb, "put_item", fail_second_write)
        payload, is_error = dispatch(
            ctx, "create_incident", {"severity": "SEV2", "summary": "x"}
        )
        assert not is_error
        assert payload["created"] is True
        assert payload["incident_id"] in ctx.known_incident_ids


class TestConcurrentWriters:
    def test_two_racing_writers_produce_one_incident(self, ctx, now):
        """Two Lambdas triaging the same alarm at the same instant.

        Both build an item and both attempt the conditional put. DynamoDB
        serialises them: one wins outright, the other's condition fails and it
        recovers the winner's id. Neither pages against an incident that does
        not exist, and only one incident row is created.
        """
        left = _fresh_ctx(ctx, now=now)
        right = _fresh_ctx(ctx, now=now)

        first = _open_incident(left)
        second = _open_incident(right)

        created = [r for r in (first, second) if r["created"]]
        deduped = [r for r in (first, second) if r["deduplicated"]]

        assert len(created) == 1
        assert len(deduped) == 1
        assert deduped[0]["incident_id"] == created[0]["incident_id"]

    def test_the_active_pointer_holds_exactly_one_row(self, ctx, now):
        from boto3.dynamodb.conditions import Key

        for _ in range(5):
            _open_incident(_fresh_ctx(ctx, now=now))

        fingerprint = alert_fingerprint(ctx.alert)
        response = ctx.ddb.query(
            KeyConditionExpression=Key("PK").eq(f"INCIDENT#{fingerprint}")
        )
        assert len(response["Items"]) == 1
        assert response["Items"][0]["SK"] == "ACTIVE"
