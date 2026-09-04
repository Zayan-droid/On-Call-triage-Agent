"""The checked-in IAM policies and the scripts that render them.

Checking policies into the repo is only worth anything if they are actually
correct, so these tests assert the two properties that matter: they are valid
JSON with every placeholder substituted, and they are scoped rather than
wildcarded. A policy file with `"Action": "*"` in it would prove the opposite of
what checking it in is meant to prove.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "infra" / "iam-policies"
POLICIES = sorted(POLICY_DIR.glob("*.json"))
PLACEHOLDER = re.compile(r"\$\{(\w+)\}")

SUBSTITUTIONS = {
    "AWS_REGION": "us-east-1",
    "AWS_ACCOUNT_ID": "123456789012",
    "FUNCTION_NAME": "triage-agent",
    "TABLE_NAME": "triage-agent",
    "TOPIC_NAME": "triage-oncall-pages",
    "MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "FOUNDATION_MODEL_ID": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "SERVICE_METRICS_NAMESPACE": "OncallTriage/Services",
    "ECS_TASK_FAMILY": "triage-eval-runner",
    "ECR_REPO_NAME": "triage-eval-runner",
}

# Metric reads and the ECR auth token genuinely have no resource-level
# permissions in IAM, so `"Resource": "*"` there is a fact about AWS, not
# laziness. Every other wildcard is a finding.
WILDCARD_ALLOWED_SIDS = {
    "ReadMetricsWildcardBecauseCloudWatchHasNoResourceLevelPermissions",
    "ReadServiceMetricsAndPublishEvalScores",
    "EcrAuthTokenIsAccountWideAndCannotBeScoped",
}


def render(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    for key, value in SUBSTITUTIONS.items():
        text = text.replace(f"${{{key}}}", value)
    return json.loads(text)


def statements(document: dict) -> list[dict]:
    body = document["Statement"]
    return body if isinstance(body, list) else [body]


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
class TestPolicyDocuments:
    def test_is_valid_json_with_placeholders_intact(self, path):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["Version"] == "2012-10-17"

    def test_every_placeholder_is_known_to_the_renderer(self, path):
        """An unrecognised placeholder survives substitution and ends up in a
        live policy as the literal text `${SOMETHING}`, which silently matches
        nothing."""
        found = set(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))
        assert found <= set(SUBSTITUTIONS), f"unknown placeholders: {found - set(SUBSTITUTIONS)}"

    def test_renders_with_nothing_left_over(self, path):
        rendered = json.dumps(render(path))
        assert "${" not in rendered

    def test_every_statement_is_allow_with_a_sid(self, path):
        for statement in statements(render(path)):
            assert statement["Effect"] == "Allow"
            # A Sid is the only place a policy can explain itself -- JSON has no
            # comments, and an unexplained wildcard is indistinguishable from a
            # careless one.
            assert statement.get("Sid"), f"statement without a Sid in {path.name}"

    def test_no_wildcard_actions(self, path):
        for statement in statements(render(path)):
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            for action in actions:
                assert action != "*", f"{path.name}/{statement['Sid']} grants every action"
                assert not action.endswith(":*"), (
                    f"{path.name}/{statement['Sid']} grants a whole service: {action}"
                )

    def test_wildcard_resources_are_justified_by_their_sid(self, path):
        for statement in statements(render(path)):
            resources = statement.get("Resource", [])
            resources = [resources] if isinstance(resources, str) else resources
            if "*" in resources:
                assert statement["Sid"] in WILDCARD_ALLOWED_SIDS, (
                    f"{path.name}/{statement['Sid']} uses Resource '*' without justification"
                )

    def test_resource_arns_name_this_account(self, path):
        for statement in statements(render(path)):
            resources = statement.get("Resource", [])
            resources = [resources] if isinstance(resources, str) else resources
            for arn in resources:
                if arn == "*" or "::foundation-model/" in arn:
                    continue  # foundation-model ARNs are account-less by design
                assert SUBSTITUTIONS["AWS_ACCOUNT_ID"] in arn, (
                    f"{path.name}/{statement['Sid']}: {arn} is not scoped to an account"
                )


class TestAgentPolicy:
    """The Lambda's own policy, checked against what the code actually calls."""

    @pytest.fixture
    def document(self):
        return render(POLICY_DIR / "triage-agent-permissions.json")

    def test_grants_exactly_the_dynamodb_actions_the_tools_use(self, document):
        granted = {
            action
            for statement in statements(document)
            for action in (
                [statement["Action"]]
                if isinstance(statement["Action"], str)
                else statement["Action"]
            )
            if action.startswith("dynamodb:")
        }
        # get_item and put_item for the dedupe, query for deploys and runbooks.
        assert granted == {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"}
        # No delete, no scan, no table administration.
        assert not any(a.startswith("dynamodb:Delete") for a in granted)
        assert "dynamodb:Scan" not in granted

    def test_bedrock_covers_the_profile_and_the_foundation_models(self, document):
        """An inference profile fans out across regions. Granting only the
        profile ARN is the most common Bedrock AccessDenied, and the error does
        not tell you that."""
        arns = [
            arn
            for statement in statements(document)
            if "bedrock:InvokeModel" in statement["Action"]
            for arn in statement["Resource"]
        ]
        assert any("inference-profile/" in arn for arn in arns)
        assert sum(1 for arn in arns if "foundation-model/" in arn) >= 2

    def test_logging_is_scoped_to_this_function(self, document):
        log_arns = [
            arn
            for statement in statements(document)
            for arn in (
                [statement["Resource"]]
                if isinstance(statement["Resource"], str)
                else statement["Resource"]
            )
            if ":logs:" in arn
        ]
        assert log_arns
        assert all("/aws/lambda/triage-agent" in arn for arn in log_arns)

    def test_sns_publish_is_scoped_to_one_topic(self, document):
        publish = [s for s in statements(document) if s["Action"] == "sns:Publish"]
        assert len(publish) == 1
        assert publish[0]["Resource"].endswith(":triage-oncall-pages")

    def test_no_iam_or_lambda_administration(self, document):
        actions = " ".join(json.dumps(s["Action"]) for s in statements(document))
        for forbidden in ("iam:", "lambda:", "sts:AssumeRole", "dynamodb:DeleteTable"):
            assert forbidden not in actions


class TestTrustPolicies:
    def test_lambda_trust_is_confused_deputy_safe(self):
        """`aws:SourceAccount` stops another account's Lambda service principal
        from assuming this role."""
        document = render(POLICY_DIR / "lambda-trust-policy.json")
        statement = statements(document)[0]
        assert statement["Principal"]["Service"] == "lambda.amazonaws.com"
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"]

    def test_ecs_trust_is_scoped_to_this_accounts_ecs(self):
        document = render(POLICY_DIR / "ecs-tasks-trust-policy.json")
        condition = statements(document)[0]["Condition"]
        assert condition["StringEquals"]["aws:SourceAccount"]
        assert condition["ArnLike"]["aws:SourceArn"].startswith("arn:aws:ecs:")


def _find_bash() -> str | None:
    """A POSIX bash that actually runs.

    `shutil.which("bash")` on Windows finds `C:\\Windows\\System32\\bash.exe`,
    which is the WSL launcher -- it returns UTF-16 error text and a non-zero
    exit when no distribution is installed. Every candidate is therefore probed
    rather than trusted, and Git Bash is preferred where it exists.
    """
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


BASH = _find_bash()


@pytest.mark.skipif(BASH is None, reason="no working POSIX bash on this machine")
class TestShellScripts:
    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, *args], capture_output=True, text=True, timeout=60, cwd=str(ROOT)
        )

    @pytest.mark.parametrize("script", ["setup.sh", "teardown.sh"])
    def test_parses(self, script):
        result = self._run("-n", f"infra/{script}")
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("script", ["setup.sh", "teardown.sh"])
    def test_usage_runs_without_touching_aws(self, script):
        """Running with no arguments must print help, never start creating
        things. A setup script that acts on an empty argument list is one
        stray Enter away from a surprise bill."""
        result = self._run(f"infra/{script}")
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout

    def test_setup_rejects_an_unknown_step(self):
        result = self._run("infra/setup.sh", "delete-everything")
        assert result.returncode != 0
        assert "unknown step" in result.stderr

    def test_setup_lists_every_step_it_dispatches(self):
        """A step that exists but is not in the usage text is a step nobody
        runs."""
        source = (ROOT / "infra" / "setup.sh").read_text(encoding="utf-8")
        defined = set(re.findall(r"^step_(\w+)\(\)", source, re.MULTILINE))
        dispatched = set(
            re.split(r"\|", re.search(r"^\s+(preflight\|[\w|]+)\)", source, re.MULTILINE).group(1))
        )
        assert defined == dispatched, f"defined but not dispatched: {defined ^ dispatched}"


class TestSeeder:
    def test_dry_run_writes_nothing(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from infra.seed_data import main

        assert main(["--dry-run", "--case", "clear_001"]) == 0

    def test_it_uses_the_same_world_as_the_offline_harness(self):
        """If the seeder built its own fixtures, an offline score would say
        nothing about the deployed agent."""
        source = (ROOT / "infra" / "seed_data.py").read_text(encoding="utf-8")
        assert "from eval import world" in source
        assert "world.seed_dynamodb" in source
        assert "world.seed_cloudwatch" in source
