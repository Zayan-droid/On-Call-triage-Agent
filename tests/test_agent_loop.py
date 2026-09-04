"""The Converse tool-use loop.

What is being pinned here is mostly failure behaviour: what the agent does when
the model throttles, when it never stops asking for tools, when it returns prose
instead of JSON, and when it claims to have done something it did not do. The
happy path is covered by every eval run.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.agent import _converse, _extract_json, run_triage
from agent.tools import TOOL_NAMES
from eval.fake_bedrock import (
    POLICY_GOOD,
    POLICY_LAZY,
    POLICY_NEVER_STOPS,
    POLICY_NO_JSON,
    POLICY_PHANTOM_INCIDENT,
    POLICY_TRIGGER_HAPPY,
    ScriptedBedrock,
)


def _run(alert, cfg, stack, now, policy=POLICY_GOOD, **kwargs):
    return run_triage(
        alert,
        cfg=cfg,
        bedrock=ScriptedBedrock(policy=policy, **kwargs),
        ddb=stack["table"],
        cw=stack["cw"],
        sns=stack["sns"],
        correlation_id="loop-test",
        now=now,
        sleeper=lambda _s: None,
    )


# --------------------------------------------------------------------------
# Verdict parsing
# --------------------------------------------------------------------------


class TestExtractJson:
    def test_bare_object(self):
        assert _extract_json('{"decision": "PAGE"}') == {"decision": "PAGE"}

    def test_fenced_block(self):
        text = 'Here is my answer:\n```json\n{"decision": "NOISE"}\n```\nDone.'
        assert _extract_json(text) == {"decision": "NOISE"}

    def test_unfenced_block_with_prose_around_it(self):
        text = 'I investigated. {"decision": "PAGE", "severity": "SEV1"} That is all.'
        assert _extract_json(text)["severity"] == "SEV1"

    def test_prefers_the_last_object(self):
        text = '{"decision": "NOISE"} ... on reflection: {"decision": "PAGE"}'
        assert _extract_json(text)["decision"] == "PAGE"

    def test_braces_inside_strings_do_not_break_matching(self):
        text = '{"reasoning": "the value was {high} and }} weird", "decision": "NOISE"}'
        assert _extract_json(text)["decision"] == "NOISE"

    def test_nested_objects(self):
        text = '{"decision": "PAGE", "detail": {"a": {"b": 1}}}'
        assert _extract_json(text)["detail"]["a"]["b"] == 1

    @pytest.mark.parametrize("text", ["", "no json here at all", "{not valid", "[1,2,3]"])
    def test_returns_none_when_there_is_nothing_to_parse(self, text):
        assert _extract_json(text) is None


# --------------------------------------------------------------------------
# Decision derived from side effects
# --------------------------------------------------------------------------


class TestDecisionFromSideEffects:
    def test_page_requires_an_actual_page_call(
        self, alert, cfg, aws_stack, now, seeded_metrics, seeded_runbooks
    ):
        seeded_metrics("checkout-api", "CPUUtilization", [40] * 5 + [94] * 25)
        result = _run(alert, cfg, aws_stack, now)
        assert result.paged is True
        assert result.decision == "PAGE"
        assert "page_oncall" in result.tool_names()

    def test_no_tools_means_noise(self, alert, cfg, aws_stack, now):
        result = _run(alert, cfg, aws_stack, now, policy=POLICY_LAZY)
        assert result.decision == "NOISE"
        assert result.paged is False
        assert result.trace == []

    def test_claiming_page_without_paging_is_recorded_as_inconsistent(
        self, alert, cfg, aws_stack, now
    ):
        """The model says PAGE; the guardrail blocked the call. Reality wins."""
        result = _run(alert, cfg, aws_stack, now, policy=POLICY_PHANTOM_INCIDENT)
        assert result.self_reported_decision == "PAGE"
        assert result.paged is False
        assert result.decision == "NOISE"
        assert result.decision_consistent is False

    def test_unparseable_output_keeps_the_text_as_reasoning(
        self, alert, cfg, aws_stack, now
    ):
        result = _run(alert, cfg, aws_stack, now, policy=POLICY_NO_JSON)
        assert result.self_reported_decision is None
        assert "look at it in the morning" in result.reasoning

    def test_incident_only_when_an_incident_exists_but_no_page(
        self, alert, cfg, aws_stack, now, seeded_metrics, seeded_runbooks
    ):
        # Metric recovered: the reference policy opens a record and stays quiet.
        seeded_metrics("checkout-api", "CPUUtilization", [94] * 20 + [40] * 10)
        result = _run(alert, cfg, aws_stack, now)
        assert result.decision == "INCIDENT_ONLY"
        assert result.paged is False
        assert result.incident_id is not None


# --------------------------------------------------------------------------
# Loop control
# --------------------------------------------------------------------------


class TestLoopControl:
    def test_iteration_cap_still_produces_a_decision(self, alert, cfg, aws_stack, now):
        """A model that never stops must not yield a null result.

        A null decision scores as 'did not page', which silently inflates the
        false-negative rate with what is really a harness failure.
        """
        cfg = dataclasses.replace(cfg, max_tool_iterations=3)
        result = _run(alert, cfg, aws_stack, now, policy=POLICY_NEVER_STOPS)

        assert result.hit_iteration_cap is True
        assert result.forced_decision is True
        assert result.iterations == 3
        assert result.self_reported_decision == "NOISE"
        assert result.error is None

    def test_forced_turn_drops_the_tool_config(self, alert, cfg, aws_stack, now):
        seen = []
        cfg = dataclasses.replace(cfg, max_tool_iterations=2)
        bedrock = ScriptedBedrock(
            policy=POLICY_NEVER_STOPS, hook=lambda kw: seen.append("toolConfig" in kw)
        )
        run_triage(
            alert,
            cfg=cfg,
            bedrock=bedrock,
            ddb=aws_stack["table"],
            cw=aws_stack["cw"],
            correlation_id="x",
            now=now,
            sleeper=lambda _s: None,
        )
        # Tools offered on the loop turns, withheld on the final forced turn.
        assert seen[:-1] == [True] * (len(seen) - 1)
        assert seen[-1] is False

    def test_every_tool_use_gets_a_tool_result(self, alert, cfg, aws_stack, now, seeded_runbooks):
        """Bedrock rejects the next request if any toolUseId is unanswered."""
        captured = []
        bedrock = ScriptedBedrock(
            policy=POLICY_GOOD, hook=lambda kw: captured.append(kw["messages"])
        )
        run_triage(
            alert,
            cfg=cfg,
            bedrock=bedrock,
            ddb=aws_stack["table"],
            cw=aws_stack["cw"],
            correlation_id="x",
            now=now,
            sleeper=lambda _s: None,
        )
        final = captured[-1]
        requested = {
            block["toolUse"]["toolUseId"]
            for message in final
            for block in message.get("content", [])
            if "toolUse" in block
        }
        answered = {
            block["toolResult"]["toolUseId"]
            for message in final
            for block in message.get("content", [])
            if "toolResult" in block
        }
        assert requested == answered

    def test_tool_errors_are_returned_with_error_status(
        self, alert, cfg, aws_stack, now
    ):
        captured = []
        bedrock = ScriptedBedrock(
            policy=POLICY_PHANTOM_INCIDENT, hook=lambda kw: captured.append(kw["messages"])
        )
        run_triage(
            alert,
            cfg=cfg,
            bedrock=bedrock,
            ddb=aws_stack["table"],
            cw=aws_stack["cw"],
            correlation_id="x",
            now=now,
            sleeper=lambda _s: None,
        )
        statuses = [
            block["toolResult"].get("status")
            for message in captured[-1]
            for block in message.get("content", [])
            if "toolResult" in block
        ]
        assert "error" in statuses

    def test_tokens_and_model_calls_accumulate(
        self, alert, cfg, aws_stack, now, seeded_runbooks
    ):
        result = _run(alert, cfg, aws_stack, now)
        assert result.model_calls >= 2
        assert result.input_tokens > 0
        assert result.output_tokens > 0


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------


class TestRetry:
    def test_throttling_is_retried(self, alert, cfg, aws_stack, now, seeded_runbooks):
        result = _run(alert, cfg, aws_stack, now, throttle_times=2)
        assert result.error is None
        assert result.decision in ("PAGE", "INCIDENT_ONLY", "NOISE")

    def test_retries_are_bounded(self, alert, cfg, aws_stack, now):
        result = _run(alert, cfg, aws_stack, now, throttle_times=99)
        assert result.error is not None
        assert "Throttling" in result.error

    def test_a_validation_error_is_not_retried(self):
        """Retrying a malformed request just burns the same error five times."""
        calls = {"n": 0}

        class Client:
            def converse(self, **kwargs):
                calls["n"] += 1
                error = Exception("bad request")
                error.response = {"Error": {"Code": "ValidationException"}}
                raise error

        with pytest.raises(Exception, match="bad request"):
            _converse(
                Client(),
                model_id="m",
                system=[],
                messages=[],
                inference={},
                tools=None,
                sleeper=lambda _s: None,
            )
        assert calls["n"] == 1

    def test_model_failure_preserves_the_work_already_done(
        self, alert, cfg, aws_stack, now
    ):
        """Tools that already ran had real side effects; the result must say so."""

        class HalfBroken(ScriptedBedrock):
            """Dies after the first tool call -- create_incident ran, the page
            never did. The result must report exactly that: an incident exists,
            nobody was woken, and the run is flagged degraded."""

            def converse(self, **kwargs):
                if self.calls >= 1:
                    self.calls += 1
                    raise RuntimeError("model exploded")
                return super().converse(**kwargs)

        result = run_triage(
            alert,
            cfg=cfg,
            bedrock=HalfBroken(policy=POLICY_TRIGGER_HAPPY),
            ddb=aws_stack["table"],
            cw=aws_stack["cw"],
            correlation_id="x",
            now=now,
            sleeper=lambda _s: None,
        )
        assert result.error is not None
        assert result.error_kind == "model_error"
        # create_incident ran before the model died, and that incident is real.
        assert result.tool_names() == ["create_incident"]
        assert result.incident_id is not None
        assert result.decision == "INCIDENT_ONLY"
        assert result.paged is False


# --------------------------------------------------------------------------
# Prompt wiring
# --------------------------------------------------------------------------


class TestPromptWiring:
    def test_unknown_variant_fails_loudly(self, alert, cfg, aws_stack, now):
        """A silent fallback would report a false null result for the experiment."""
        with pytest.raises(KeyError):
            run_triage(
                alert,
                cfg=cfg,
                bedrock=ScriptedBedrock(),
                ddb=aws_stack["table"],
                cw=aws_stack["cw"],
                correlation_id="x",
                now=now,
                prompt_variant="v3_does_not_exist",
            )

    def test_the_five_tools_are_offered(self, alert, cfg, aws_stack, now):
        captured = []
        run_triage(
            alert,
            cfg=cfg,
            bedrock=ScriptedBedrock(policy=POLICY_LAZY, hook=captured.append),
            ddb=aws_stack["table"],
            cw=aws_stack["cw"],
            correlation_id="x",
            now=now,
        )
        offered = {
            spec["toolSpec"]["name"] for spec in captured[0]["toolConfig"]["tools"]
        }
        assert offered == set(TOOL_NAMES)
