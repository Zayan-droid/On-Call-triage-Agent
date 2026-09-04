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
TASK_DEFINITION = ROOT / "infra" / "ecs-task-definition.json"
RENDERED_FILES = [*POLICIES, TASK_DEFINITION]
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
    "ECS_EXECUTION_ROLE_NAME": "triage-eval-runner-execution-role",
    "ECS_TASK_ROLE_NAME": "triage-eval-runner-task-role",
    "AGENTCORE_REPO_NAME": "triage-agentcore",
    "AGENTCORE_RUNTIME_NAME": "triage_agent",
    "AGENT_METRICS_NAMESPACE": "OncallTriage",
    "PROMPT_VARIANT": "v2_investigate_first",
    "IMAGE_TAG": "latest",
}

# Metric reads and the ECR auth token genuinely have no resource-level
# permissions in IAM, so `"Resource": "*"` there is a fact about AWS, not
# laziness. Every other wildcard is a finding.
WILDCARD_ALLOWED_SIDS = {
    "ReadMetricsWildcardBecauseCloudWatchHasNoResourceLevelPermissions",
    "ReadServiceMetricsAndPublishEvalScores",
    "EcrAuthTokenIsAccountWideAndCannotBeScoped",
    "XRayHasNoResourceLevelPermissions",
    "PutMetricDataIsNamespaceScopedByConditionNotByArn",
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
                [candidate, "-c", "echo ok"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
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
            [BASH, *args],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
            check=False,
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


# --------------------------------------------------------------------------
# The two container deployment targets
# --------------------------------------------------------------------------


class TestSetupRenderer:
    """The shell renderer and the policy files have to agree.

    This is the test that earns its keep. `render_policy` in setup.sh is a list
    of sed expressions, and adding a placeholder to a policy file without
    adding it there leaves the literal text `${SOMETHING}` in a live IAM
    policy or task definition -- which is valid JSON, is accepted by AWS, and
    silently matches nothing.
    """

    @staticmethod
    def renderer_handles() -> set[str]:
        source = (ROOT / "infra" / "setup.sh").read_text(encoding="utf-8")
        body = source[source.index("render_policy() {") : source.index("ensure_role() {")]
        return set(re.findall(r"\$\\?\{(\w+)\}\|", body))

    def test_setup_substitutes_every_placeholder_used_anywhere(self):
        used: set[str] = set()
        for path in RENDERED_FILES:
            used |= set(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))
        missing = used - self.renderer_handles()
        assert not missing, f"setup.sh render_policy does not substitute: {sorted(missing)}"

    def test_the_test_suite_and_the_shell_agree_on_the_placeholder_set(self):
        """Otherwise these tests could pass against a set of names the deploy
        script has never heard of."""
        assert self.renderer_handles() <= set(SUBSTITUTIONS), (
            f"setup.sh substitutes names the tests do not know: "
            f"{sorted(self.renderer_handles() - set(SUBSTITUTIONS))}"
        )


class TestEcsTaskDefinition:
    @pytest.fixture
    def task_def(self):
        return render(TASK_DEFINITION)

    def test_is_a_fargate_awsvpc_task(self, task_def):
        assert task_def["requiresCompatibilities"] == ["FARGATE"]
        # Fargate rejects anything else, and the error names the field without
        # explaining that the two are coupled.
        assert task_def["networkMode"] == "awsvpc"
        assert task_def["runtimePlatform"]["cpuArchitecture"] == "X86_64"

    def test_execution_and_task_roles_are_different_roles(self, task_def):
        """The distinction is the whole point of learning ECS IAM: one is what
        ECS may do to start the task, the other is what the code inside may do.
        A task definition naming the same role twice has collapsed them."""
        assert task_def["executionRoleArn"] != task_def["taskRoleArn"]
        assert "execution" in task_def["executionRoleArn"]

    def test_the_image_comes_from_this_accounts_ecr(self, task_def):
        image = task_def["containerDefinitions"][0]["image"]
        assert image.startswith(f"{SUBSTITUTIONS['AWS_ACCOUNT_ID']}.dkr.ecr.")
        assert f"/{SUBSTITUTIONS['ECR_REPO_NAME']}:" in image

    def test_the_sweep_runs_against_aws_and_persists_its_results(self, task_def):
        command = task_def["containerDefinitions"][0]["command"]
        assert "--mode" in command and "aws" in command
        # Without these two the task burns 20 minutes of Fargate and leaves
        # nothing behind, because its filesystem goes away when it stops.
        assert "--write-dynamo" in command
        assert "--emit-metrics" in command

    def test_the_batch_runner_cannot_page_anyone(self, task_def):
        """A 38-case sweep would send 19 emails. `--send-pages` is off by
        default in the harness; this asserts nobody turned it on here, and the
        task role has no sns:Publish either, so the two agree."""
        assert "--send-pages" not in task_def["containerDefinitions"][0]["command"]
        role = render(POLICY_DIR / "eval-runner-task-role.json")
        actions = " ".join(json.dumps(s["Action"]) for s in statements(role))
        assert "sns:" not in actions

    def test_logs_go_somewhere_the_execution_role_can_write(self, task_def):
        options = task_def["containerDefinitions"][0]["logConfiguration"]["options"]
        group = options["awslogs-group"]
        allowed = [
            arn
            for statement in statements(render(POLICY_DIR / "eval-runner-execution-role.json"))
            for arn in (
                [statement["Resource"]]
                if isinstance(statement["Resource"], str)
                else statement["Resource"]
            )
            if ":logs:" in arn
        ]
        assert allowed, "the execution role cannot write any log group"
        assert any(group in arn or arn.rstrip("*") in group for arn in allowed)


class TestAgentCorePolicies:
    def test_trust_names_the_agentcore_service_principal(self):
        statement = statements(render(POLICY_DIR / "agentcore-trust-policy.json"))[0]
        assert statement["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
        condition = statement["Condition"]
        assert condition["StringEquals"]["aws:SourceAccount"]
        assert condition["ArnLike"]["aws:SourceArn"].startswith("arn:aws:bedrock-agentcore:")

    def test_the_runtime_role_can_do_everything_the_lambda_can(self):
        """Both deployments run the same code, so a permission the Lambda has
        and the runtime does not is a deployment that fails at the first tool
        call rather than at deploy time."""
        lambda_role = render(POLICY_DIR / "triage-agent-permissions.json")
        runtime_role = render(POLICY_DIR / "agentcore-runtime-role.json")

        def actions(document):
            return {
                action
                for statement in statements(document)
                for action in (
                    [statement["Action"]]
                    if isinstance(statement["Action"], str)
                    else statement["Action"]
                )
                if not action.startswith("logs:")  # log groups differ by design
            }

        missing = actions(lambda_role) - actions(runtime_role)
        assert not missing, f"the AgentCore role is missing: {sorted(missing)}"

    def test_put_metric_data_is_constrained_by_namespace(self):
        """cloudwatch:PutMetricData has no resource-level permission, so the
        only way to scope it at all is a namespace condition. Without one this
        role can write to any namespace in the account."""
        statement = next(
            s
            for s in statements(render(POLICY_DIR / "agentcore-runtime-role.json"))
            if s["Action"] == "cloudwatch:PutMetricData"
        )
        assert statement["Resource"] == "*"
        namespaces = statement["Condition"]["StringEquals"]["cloudwatch:namespace"]
        assert "bedrock-agentcore" in namespaces

    def test_workload_identity_is_scoped_to_this_runtime(self):
        statement = next(
            s
            for s in statements(render(POLICY_DIR / "agentcore-runtime-role.json"))
            if "bedrock-agentcore:GetWorkloadAccessToken" in s["Action"]
        )
        assert any(
            SUBSTITUTIONS["AGENTCORE_RUNTIME_NAME"] in arn for arn in statement["Resource"]
        )


DOCKERFILES = {
    "eval-runner": ROOT / "infra" / "Dockerfile.eval-runner",
    "agentcore": ROOT / "infra" / "Dockerfile.agentcore",
}


class TestDockerfiles:
    @pytest.mark.parametrize("name", sorted(DOCKERFILES))
    def test_runs_as_a_non_root_user(self, name):
        """Nothing in either container needs root. AWS access comes from the
        task or execution role, not from the container user."""
        body = DOCKERFILES[name].read_text(encoding="utf-8")
        assert re.search(r"^USER (?!root|0$)\S+", body, re.MULTILINE), "no non-root USER"

    @pytest.mark.parametrize("name", sorted(DOCKERFILES))
    def test_base_image_is_pinned_to_a_minor_version(self, name):
        """`python:latest` would silently change the interpreter under a
        deployed agent."""
        body = DOCKERFILES[name].read_text(encoding="utf-8")
        base = re.search(r"^FROM.*?(\S+/python:\S+)", body, re.MULTILINE).group(1)
        assert ":latest" not in base
        assert re.search(r"python:3\.\d+", base), base

    @pytest.mark.parametrize("name", sorted(DOCKERFILES))
    def test_the_build_context_excludes_secrets_and_the_virtualenv(self, name):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".venv/", ".env", "*.pem", ".git/"):
            assert pattern in ignored, f"{pattern} is not in .dockerignore"

    def test_the_eval_runner_ships_the_harness_but_not_the_tests(self):
        body = DOCKERFILES["eval-runner"].read_text(encoding="utf-8")
        assert "COPY agent/" in body
        assert "COPY eval/" in body
        assert "COPY tests/" not in body
        # The entrypoint is the harness itself, so the task definition supplies
        # the flags and a different sweep does not need a rebuild.
        assert '"eval.run"' in body

    def test_the_agentcore_image_serves_http_and_ships_only_the_agent(self):
        body = DOCKERFILES["agentcore"].read_text(encoding="utf-8")
        assert "EXPOSE 8080" in body
        assert '"agent.server"' in body
        # The eval harness has no business inside the deployed agent.
        assert "COPY eval/" not in body

    def test_every_builder_asks_for_arm64_and_the_dockerfile_does_not_pin_it(self):
        """AgentCore Runtime accepts linux/arm64 only, and rejects an amd64
        image with an error that never mentions architecture.

        The architecture is asserted where it is actually decided -- in the
        builders -- rather than in the Dockerfile. A constant
        `FROM --platform=...` overrides whatever the build requested, which is
        how a multi-platform build silently yields one architecture, and
        BuildKit lints it (FromPlatformFlagConstDisallowed). Asserting the old
        line would have pinned exactly the thing that needed removing.
        """
        body = DOCKERFILES["agentcore"].read_text(encoding="utf-8")
        from_line = next(line for line in body.splitlines() if line.startswith("FROM"))
        assert "--platform" not in from_line, from_line

        # The deploy script must request it, in both the buildx and the
        # fallback path.
        setup = (ROOT / "infra" / "setup.sh").read_text(encoding="utf-8")
        step = setup[setup.index("step_agentcore_image()") : setup.index("agentcore_runtime_field()")]
        builds = [
            line.strip()
            for line in step.splitlines()
            if line.strip().startswith(("docker build", "docker buildx build"))
        ]
        # Two: the buildx path, and the plain-build fallback for a host without it.
        assert len(builds) == 2, builds
        assert all("--platform linux/arm64" in line for line in builds), builds

        # And so must CI, or the image it health-checks is not the one deployed.
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        agentcore_job = ci[ci.index("Build the AgentCore image") :]
        assert "platforms: linux/arm64" in agentcore_job.split("- name:")[0]


class TestTeardownCoversEverySetupTarget:
    @pytest.fixture
    def scripts(self):
        return (
            (ROOT / "infra" / "setup.sh").read_text(encoding="utf-8"),
            (ROOT / "infra" / "teardown.sh").read_text(encoding="utf-8"),
        )

    def test_every_named_resource_in_setup_is_named_in_teardown(self, scripts):
        """A resource setup.sh can create and teardown.sh cannot delete is a
        bill nobody notices."""
        setup, teardown = scripts
        names = set(re.findall(r'^([A-Z_]+)="\$\{\1:-([^}]*)\}"', setup, re.MULTILINE))
        # Only the ones that name a billable AWS resource.
        billable = {
            name
            for name, _ in names
            if any(
                token in name
                for token in ("ROLE_NAME", "CLUSTER", "REPO_NAME", "RUNTIME_NAME", "TASK_FAMILY")
            )
        }
        assert billable, "the name scan found nothing; the regex has drifted"
        missing = {name for name in billable if name not in teardown}
        assert not missing, f"teardown.sh never references: {sorted(missing)}"

    def test_teardown_all_deletes_images_after_the_things_that_reference_them(self, scripts):
        _, teardown = scripts
        order = teardown[teardown.index("step_api; step_lambda") :]
        line = order.split("\n")[0] + order.split("\n")[1]
        assert line.index("step_agentcore") < line.index("step_ecr")
        assert line.index("step_ecs") < line.index("step_ecr")
