#!/usr/bin/env bash
#
# Create every AWS resource this project needs, one step at a time.
#
#   bash infra/setup.sh                 # show the steps
#   bash infra/setup.sh preflight       # run one step
#   bash infra/setup.sh all             # run them in order
#
# Every step is idempotent: re-running it either does nothing or updates in
# place. That matters more than it sounds -- you will run these repeatedly while
# something further down the list is failing, and a step that half-succeeds and
# then refuses to run again is a step you end up debugging instead of the thing
# that was actually broken.
#
# Run `bash infra/teardown.sh all` when you are done. Custom CloudWatch metrics
# and an idle DynamoDB table are the two things here that quietly cost money.
#
# Runs in Git Bash, WSL, macOS, Linux, or CloudShell. Requires: aws, python3, jq.

set -euo pipefail

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY_DIR="$ROOT/infra/iam-policies"

# Anything here can be overridden from the environment or an untracked
# infra/.env -- which is why .env is in .gitignore.
[ -f "$ROOT/infra/.env" ] && . "$ROOT/infra/.env"

export AWS_REGION="${AWS_REGION:-us-east-1}"
FUNCTION_NAME="${FUNCTION_NAME:-triage-agent}"
ROLE_NAME="${ROLE_NAME:-triage-agent-role}"
POLICY_NAME="${POLICY_NAME:-triage-agent-permissions}"
TABLE_NAME="${TABLE_NAME:-triage-agent}"
TOPIC_NAME="${TOPIC_NAME:-triage-oncall-pages}"
API_NAME="${API_NAME:-triage-agent-api}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
SERVICE_METRICS_NAMESPACE="${SERVICE_METRICS_NAMESPACE:-OncallTriage/Services}"
AGENT_METRICS_NAMESPACE="${AGENT_METRICS_NAMESPACE:-OncallTriage}"
EVAL_METRICS_NAMESPACE="${EVAL_METRICS_NAMESPACE:-OncallTriage/Eval}"

# The inference profile id, and the bare model id the profile fans out to. IAM
# needs BOTH: the profile ARN in your account, and the foundation-model ARN in
# every region the profile can route to. Granting only the profile is the single
# most common Bedrock AccessDenied, and the error message does not say so.
MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
FOUNDATION_MODEL_ID="${FOUNDATION_MODEL_ID:-anthropic.claude-sonnet-4-5-20250929-v1:0}"

PAGER_EMAIL="${PAGER_EMAIL:-}"
BUILD_DIR="$ROOT/build"

# ---- container deployment targets (ECS Fargate + AgentCore Runtime) ----
# Both are separate deploy targets rather than part of `all`: each needs Docker,
# each pushes an image, and neither is required to run or demonstrate the
# serverless system. Grouped runs are `ecs_all` and `agentcore_all`.
ECR_REPO_NAME="${ECR_REPO_NAME:-triage-eval-runner}"
ECS_TASK_FAMILY="${ECS_TASK_FAMILY:-triage-eval-runner}"
ECS_CLUSTER_NAME="${ECS_CLUSTER_NAME:-triage-eval}"
ECS_EXECUTION_ROLE_NAME="${ECS_EXECUTION_ROLE_NAME:-triage-eval-runner-execution-role}"
ECS_TASK_ROLE_NAME="${ECS_TASK_ROLE_NAME:-triage-eval-runner-task-role}"
AGENTCORE_REPO_NAME="${AGENTCORE_REPO_NAME:-triage-agentcore}"
AGENTCORE_RUNTIME_NAME="${AGENTCORE_RUNTIME_NAME:-triage_agent}"
AGENTCORE_ROLE_NAME="${AGENTCORE_ROLE_NAME:-triage-agentcore-role}"
PROMPT_VARIANT="${PROMPT_VARIANT:-v2_investigate_first}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Kept as a variable rather than inline JSON inside a function, where its
# braces read as shell expansion to anyone skimming the file.
ECR_LIFECYCLE_POLICY='{"rules":[{"rulePriority":1,
  "description":"Expire untagged images after 1 day",
  "selection":{"tagStatus":"untagged","countType":"sinceImagePushed",
               "countUnit":"days","countNumber":1},
  "action":{"type":"expire"}}]}'

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*" >&2; }
die()   { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

account_id() { aws sts get-caller-identity --query Account --output text; }

# --------------------------------------------------------------------------
# 1. preflight
# --------------------------------------------------------------------------

step_preflight() {
  blue "== preflight =="
  for tool in aws python3 jq; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
  done

  local caller
  caller="$(aws sts get-caller-identity --output json)" || die "no working AWS credentials"
  echo "$caller" | jq -r '"  identity : \(.Arn)\n  account  : \(.Account)"'
  echo "  region   : $AWS_REGION"

  # Bedrock model access is granted per account per region and is the one thing
  # here you cannot fix with a policy -- it needs a click in the console.
  blue "  checking Bedrock model access..."
  if aws bedrock list-foundation-models --region "$AWS_REGION" \
      --query "modelSummaries[?modelId=='$FOUNDATION_MODEL_ID'].modelId" \
      --output text 2>/dev/null | grep -q .; then
    green "  model $FOUNDATION_MODEL_ID is visible in $AWS_REGION"
  else
    warn "  could not confirm access to $FOUNDATION_MODEL_ID in $AWS_REGION."
    warn "  Bedrock console -> Model access -> enable Anthropic models, then re-run."
  fi
  green "preflight ok"
}

# --------------------------------------------------------------------------
# 2. iam
# --------------------------------------------------------------------------

render_policy() {
  # Substitute the ${...} placeholders in a checked-in policy file. The files
  # stay readable and reviewable in git with placeholders rather than a hard
  # coded account number.
  local account="$1" file="$2"
  sed -e "s|\${AWS_REGION}|$AWS_REGION|g" \
      -e "s|\${AWS_ACCOUNT_ID}|$account|g" \
      -e "s|\${FUNCTION_NAME}|$FUNCTION_NAME|g" \
      -e "s|\${TABLE_NAME}|$TABLE_NAME|g" \
      -e "s|\${TOPIC_NAME}|$TOPIC_NAME|g" \
      -e "s|\${MODEL_ID}|$MODEL_ID|g" \
      -e "s|\${FOUNDATION_MODEL_ID}|$FOUNDATION_MODEL_ID|g" \
      -e "s|\${SERVICE_METRICS_NAMESPACE}|$SERVICE_METRICS_NAMESPACE|g" \
      -e "s|\${AGENT_METRICS_NAMESPACE}|$AGENT_METRICS_NAMESPACE|g" \
      -e "s|\${ECS_TASK_FAMILY}|$ECS_TASK_FAMILY|g" \
      -e "s|\${ECR_REPO_NAME}|$ECR_REPO_NAME|g" \
      -e "s|\${ECS_EXECUTION_ROLE_NAME}|$ECS_EXECUTION_ROLE_NAME|g" \
      -e "s|\${ECS_TASK_ROLE_NAME}|$ECS_TASK_ROLE_NAME|g" \
      -e "s|\${AGENTCORE_REPO_NAME}|$AGENTCORE_REPO_NAME|g" \
      -e "s|\${AGENTCORE_RUNTIME_NAME}|$AGENTCORE_RUNTIME_NAME|g" \
      -e "s|\${PROMPT_VARIANT}|$PROMPT_VARIANT|g" \
      -e "s|\${IMAGE_TAG}|$IMAGE_TAG|g" \
      "$file"
}

ensure_role() {
  # Create the role, or update the trust policy of the one that is already
  # there. Every role in this script goes through here so that re-running a
  # step never fails with EntityAlreadyExists.
  local name="$1" trust="$2" description="$3"
  if aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    echo "  role $name exists; updating its trust policy"
    aws iam update-assume-role-policy --role-name "$name" \
      --policy-document "$trust" >/dev/null
  else
    echo "  creating role $name"
    aws iam create-role --role-name "$name" \
      --assume-role-policy-document "$trust" \
      --description "$description" >/dev/null
  fi
}

step_iam() {
  blue "== iam =="
  local account trust perms
  account="$(account_id)"
  trust="$(render_policy "$account" "$POLICY_DIR/lambda-trust-policy.json")"
  perms="$(render_policy "$account" "$POLICY_DIR/triage-agent-permissions.json")"

  ensure_role "$ROLE_NAME" "$trust" "Execution role for the triage agent Lambda"

  # An inline policy rather than a managed one: this policy is meaningless
  # outside this role, and inline means it cannot be left attached to something
  # else after teardown.
  echo "  putting inline policy $POLICY_NAME"
  aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" --policy-document "$perms" >/dev/null

  green "  role arn: arn:aws:iam::$account:role/$ROLE_NAME"
  warn "  IAM is eventually consistent. If the next step fails with"
  warn "  'The role defined for the function cannot be assumed', wait ~10s and retry."
}

# --------------------------------------------------------------------------
# 3. sns
# --------------------------------------------------------------------------

step_sns() {
  blue "== sns =="
  local arn
  arn="$(aws sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text)"
  green "  topic: $arn"

  if [ -n "$PAGER_EMAIL" ]; then
    aws sns subscribe --topic-arn "$arn" --protocol email \
      --notification-endpoint "$PAGER_EMAIL" >/dev/null
    warn "  Confirm the subscription from the email AWS just sent to $PAGER_EMAIL."
    warn "  Until you click it, SNS accepts publishes and delivers nothing."
  else
    warn "  PAGER_EMAIL is unset, so nothing is subscribed and no page will arrive."
    warn "  export PAGER_EMAIL=you@example.com and re-run this step."
  fi
}

# --------------------------------------------------------------------------
# 4. dynamodb
# --------------------------------------------------------------------------

step_dynamodb() {
  blue "== dynamodb =="
  if aws dynamodb describe-table --table-name "$TABLE_NAME" >/dev/null 2>&1; then
    echo "  table $TABLE_NAME already exists"
  else
    echo "  creating table $TABLE_NAME (PK/SK, on-demand billing)"
    aws dynamodb create-table --table-name "$TABLE_NAME" \
      --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
      --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
      --billing-mode PAY_PER_REQUEST >/dev/null
    aws dynamodb wait table-exists --table-name "$TABLE_NAME"
  fi

  # TTL so incidents and eval runs expire on their own. Without it this table
  # grows forever, and the storage is the only part of DynamoDB that costs
  # anything when nothing is reading it.
  local status
  status="$(aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)"
  if [ "$status" != "ENABLED" ] && [ "$status" != "ENABLING" ]; then
    aws dynamodb update-time-to-live --table-name "$TABLE_NAME" \
      --time-to-live-specification "Enabled=true,AttributeName=ttl" >/dev/null
    echo "  TTL enabled on attribute 'ttl'"
  else
    echo "  TTL already $status"
  fi
  green "  table ready"
}

# --------------------------------------------------------------------------
# 5. lambda
# --------------------------------------------------------------------------

step_package() {
  blue "== package =="
  rm -rf "$BUILD_DIR"
  mkdir -p "$BUILD_DIR"
  # Only the agent package ships. boto3 is already in the Lambda runtime, and
  # bundling it would add ~15MB to the artifact and slow every cold start for
  # no benefit. Python's zipfile is used rather than `zip` so this works
  # identically on Windows, where `zip` is often not installed.
  python3 - "$ROOT" "$BUILD_DIR/function.zip" <<'PY'
import pathlib, sys, zipfile

root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((root / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        archive.write(path, path.relative_to(root).as_posix())
print(f"  {out.name}: {out.stat().st_size / 1024:.1f} KB, "
      f"{len(zipfile.ZipFile(out).namelist())} files")
PY
  green "  packaged"
}

step_lambda() {
  blue "== lambda =="
  [ -f "$BUILD_DIR/function.zip" ] || step_package

  local account role topic env_vars
  account="$(account_id)"
  role="arn:aws:iam::$account:role/$ROLE_NAME"
  topic="arn:aws:sns:$AWS_REGION:$account:$TOPIC_NAME"

  env_vars="Variables={TRIAGE_TABLE_NAME=$TABLE_NAME,TRIAGE_SNS_TOPIC_ARN=$topic"
  env_vars="$env_vars,TRIAGE_MODEL_ID=$MODEL_ID"
  env_vars="$env_vars,TRIAGE_METRICS_NAMESPACE=$AGENT_METRICS_NAMESPACE"
  env_vars="$env_vars,TRIAGE_SERVICE_METRICS_NAMESPACE=$SERVICE_METRICS_NAMESPACE"
  env_vars="$env_vars,TRIAGE_PROMPT_VARIANT=${PROMPT_VARIANT:-v2_investigate_first}"
  env_vars="$env_vars,TRIAGE_LOG_LEVEL=${TRIAGE_LOG_LEVEL:-INFO}"
  [ -n "${TRIAGE_API_KEY:-}" ] && env_vars="$env_vars,TRIAGE_API_KEY=$TRIAGE_API_KEY"
  env_vars="$env_vars}"

  if aws lambda get-function --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
    echo "  updating code"
    aws lambda update-function-code --function-name "$FUNCTION_NAME" \
      --zip-file "fileb://$BUILD_DIR/function.zip" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME"
    echo "  updating configuration"
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
      --timeout 120 --memory-size 512 --environment "$env_vars" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME"
  else
    echo "  creating function $FUNCTION_NAME"
    # 120s timeout: a Converse loop with five tool calls can genuinely take a
    # minute. 512MB is not about memory -- Lambda scales CPU with memory, and
    # at 128MB the JSON and boto3 import alone add seconds to every cold start.
    aws lambda create-function --function-name "$FUNCTION_NAME" \
      --runtime python3.12 --role "$role" --handler agent.handler.lambda_handler \
      --zip-file "fileb://$BUILD_DIR/function.zip" \
      --timeout 120 --memory-size 512 --environment "$env_vars" \
      --description "Investigates an infrastructure alert and decides whether to page" >/dev/null
    aws lambda wait function-active --function-name "$FUNCTION_NAME"
  fi

  # Logs default to never expiring, which is a slow, silent bill.
  aws logs put-retention-policy --log-group-name "/aws/lambda/$FUNCTION_NAME" \
    --retention-in-days "$LOG_RETENTION_DAYS" 2>/dev/null \
    || warn "  log group not created yet; retention will be set after the first invoke"
  green "  lambda ready"
}

# --------------------------------------------------------------------------
# 6. api gateway
# --------------------------------------------------------------------------

step_api() {
  blue "== api gateway =="
  local account api_id endpoint
  account="$(account_id)"

  api_id="$(aws apigatewayv2 get-apis \
    --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)"

  if [ "$api_id" = "None" ] || [ -z "$api_id" ]; then
    echo "  creating HTTP API $API_NAME"
    # An HTTP API rather than a REST API: this endpoint needs a route, a Lambda
    # integration and nothing else. REST APIs cost roughly 3.5x per million
    # requests and buy request validation, WAF and usage plans that this
    # project does not use.
    api_id="$(aws apigatewayv2 create-api --name "$API_NAME" \
      --protocol-type HTTP \
      --target "arn:aws:lambda:$AWS_REGION:$account:function:$FUNCTION_NAME" \
      --route-key "POST /alert" --query ApiId --output text)"
  else
    echo "  api $API_NAME exists ($api_id)"
  fi

  # Idempotent: a duplicate statement id is rejected, which is fine.
  aws lambda add-permission --function-name "$FUNCTION_NAME" \
    --statement-id apigateway-invoke \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$AWS_REGION:$account:$api_id/*/*/alert" \
    >/dev/null 2>&1 || echo "  invoke permission already present"

  endpoint="$(aws apigatewayv2 get-api --api-id "$api_id" \
    --query ApiEndpoint --output text)"
  green "  endpoint: $endpoint/alert"
  echo
  echo "  export API_URL=$endpoint"
}

# --------------------------------------------------------------------------
# 7. seed
# --------------------------------------------------------------------------

# shellcheck disable=SC2120  # called with no arguments by `all`, and with
# extra flags when invoked directly: `bash infra/setup.sh seed --dry-run`.
step_seed() {
  blue "== seed =="
  python3 "$ROOT/infra/seed_data.py" \
    --table "$TABLE_NAME" --region "$AWS_REGION" \
    --namespace "$SERVICE_METRICS_NAMESPACE" "$@"
}

# --------------------------------------------------------------------------
# 8. alarms
# --------------------------------------------------------------------------

step_alarms() {
  blue "== alarms =="
  local account topic
  account="$(account_id)"
  topic="arn:aws:sns:$AWS_REGION:$account:$TOPIC_NAME"

  # Operational: the function itself is failing.
  aws cloudwatch put-metric-alarm \
    --alarm-name "triage-agent-lambda-errors" \
    --alarm-description "The triage Lambda is throwing. Alerts are not being triaged at all." \
    --namespace AWS/Lambda --metric-name Errors \
    --dimensions "Name=FunctionName,Value=$FUNCTION_NAME" \
    --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$topic" >/dev/null
  echo "  triage-agent-lambda-errors"

  # Operational: the agent is running but its tools are failing -- an expired
  # permission, a deleted table. It will still reach a decision, on no evidence.
  aws cloudwatch put-metric-alarm \
    --alarm-name "triage-agent-tool-failures" \
    --alarm-description "Tool calls are failing. The agent is deciding without evidence." \
    --namespace "$AGENT_METRICS_NAMESPACE" --metric-name FailedToolCalls \
    --statistic Sum --period 900 --evaluation-periods 1 --threshold 5 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$topic" >/dev/null
  echo "  triage-agent-tool-failures"

  # Quality: this is the interesting one. Agent quality is an operational
  # metric here, so a regression in judgement pages exactly like a regression
  # in latency. Both alarms read the numbers `eval/run.py --emit-metrics` writes.
  aws cloudwatch put-metric-alarm \
    --alarm-name "triage-eval-tool-selection-accuracy" \
    --alarm-description "Tool selection accuracy dropped below 0.85 on the eval suite." \
    --namespace "$EVAL_METRICS_NAMESPACE" --metric-name ToolSelectionAccuracy \
    --dimensions "Name=PromptVariant,Value=${PROMPT_VARIANT:-v2_investigate_first}" \
                 "Name=Suite,Value=${EVAL_SUITE:-aws-v2_investigate_first}" \
    --statistic Average --period 86400 --evaluation-periods 1 --threshold 0.85 \
    --comparison-operator LessThanThreshold \
    --treat-missing-data missing \
    --alarm-actions "$topic" >/dev/null
  echo "  triage-eval-tool-selection-accuracy  (< 0.85)"

  aws cloudwatch put-metric-alarm \
    --alarm-name "triage-eval-false-page-rate" \
    --alarm-description "False-page rate rose above 0.10. The agent is starting to cause alert fatigue." \
    --namespace "$EVAL_METRICS_NAMESPACE" --metric-name FalsePageRate \
    --dimensions "Name=PromptVariant,Value=${PROMPT_VARIANT:-v2_investigate_first}" \
                 "Name=Suite,Value=${EVAL_SUITE:-aws-v2_investigate_first}" \
    --statistic Average --period 86400 --evaluation-periods 1 --threshold 0.10 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data missing \
    --alarm-actions "$topic" >/dev/null
  echo "  triage-eval-false-page-rate           (> 0.10)"

  green "  4 alarms configured"
  warn "  The two eval alarms stay INSUFFICIENT_DATA until a run publishes with"
  warn "  --emit-metrics. treat-missing-data is 'missing' on purpose: a suite that"
  warn "  stopped running should not look like a suite that is passing."
}

# --------------------------------------------------------------------------
# 9. smoke
# --------------------------------------------------------------------------

step_smoke() {
  blue "== smoke =="
  local api_id endpoint payload
  api_id="$(aws apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)"
  [ "$api_id" = "None" ] && die "no API named $API_NAME; run the api step first"
  endpoint="$(aws apigatewayv2 get-api --api-id "$api_id" --query ApiEndpoint --output text)"

  payload='{"alarm_name":"checkout-api-5xx-high","service":"checkout-api","environment":"prod","metric":"Error5xxRate","value":8.4,"threshold":1.0,"duration_min":14}'

  echo "  POST $endpoint/alert"
  local args=(-sS -X POST "$endpoint/alert" -H "content-type: application/json" -d "$payload")
  [ -n "${TRIAGE_API_KEY:-}" ] && args+=(-H "x-api-key: $TRIAGE_API_KEY")
  curl "${args[@]}" | jq .

  echo
  blue "  recent structured logs:"
  aws logs tail "/aws/lambda/$FUNCTION_NAME" --since 5m --format short 2>/dev/null | tail -20 \
    || warn "  no logs yet"
}

# --------------------------------------------------------------------------
# 11-14. ECS Fargate batch eval runner
#
# Why any of this exists: a 38-case sweep against real Bedrock takes 15-25
# minutes and Lambda stops at 15. That ceiling is the whole reason there is a
# container here at all -- see docs/decisions.md ADR-2.
# --------------------------------------------------------------------------

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH; the image steps need it"
  docker info >/dev/null 2>&1 || die "docker is installed but not running"
}

ecr_registry() { echo "$1.dkr.ecr.$AWS_REGION.amazonaws.com"; }


ensure_ecr_repo() {
  local name="$1"
  if aws ecr describe-repositories --repository-names "$name" >/dev/null 2>&1; then
    echo "  repo $name exists"
  else
    echo "  creating repo $name"
    # Scan on push: a base image picks up CVEs between builds and this is the
    # cheapest place to find that out. Tags stay MUTABLE on purpose -- `latest`
    # is re-pushed on every build here, and immutability would break that.
    aws ecr create-repository --repository-name "$name" \
      --image-scanning-configuration scanOnPush=true \
      --image-tag-mutability MUTABLE >/dev/null
  fi

  # Untagged layers left behind by a re-pushed tag are billed storage that
  # nothing can ever pull again. Expire them.
  aws ecr put-lifecycle-policy --repository-name "$name" \
    --lifecycle-policy-text "$ECR_LIFECYCLE_POLICY" >/dev/null
}

step_ecr() {
  blue "== ecr =="
  ensure_ecr_repo "$ECR_REPO_NAME"
  ensure_ecr_repo "$AGENTCORE_REPO_NAME"
  green "  2 repositories ready in $(ecr_registry "$(account_id)")"
}

docker_login() {
  local registry="$1"
  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null \
    || die "docker login to $registry failed"
}

step_image() {
  blue "== image (eval runner) =="
  require_docker
  local account registry uri
  account="$(account_id)"
  registry="$(ecr_registry "$account")"
  uri="$registry/$ECR_REPO_NAME:$IMAGE_TAG"

  ensure_ecr_repo "$ECR_REPO_NAME"
  docker_login "$registry"

  echo "  building $uri"
  docker build --platform linux/amd64 \
    -f "$ROOT/infra/Dockerfile.eval-runner" -t "$uri" "$ROOT" \
    || die "docker build failed"
  echo "  pushing"
  docker push "$uri" >/dev/null || die "docker push failed"
  green "  pushed $uri"
}

step_ecs() {
  blue "== ecs =="
  local account trust exec_perms task_perms task_def revision
  account="$(account_id)"

  # Two roles, and the difference between them is the thing actually worth
  # understanding about ECS:
  #
  #   execution role  what ECS ITSELF may do on the task's behalf before the
  #                   container starts -- pull the image, write to the log group.
  #   task role       what the CODE INSIDE the container may do once it is
  #                   running -- Bedrock, DynamoDB, CloudWatch.
  #
  # Collapsing them into one role is the standard mistake. It hands the
  # container permission to pull arbitrary images from the registry, which is
  # not a thing the eval sweep has any business doing.
  trust="$(render_policy "$account" "$POLICY_DIR/ecs-tasks-trust-policy.json")"
  exec_perms="$(render_policy "$account" "$POLICY_DIR/eval-runner-execution-role.json")"
  task_perms="$(render_policy "$account" "$POLICY_DIR/eval-runner-task-role.json")"

  ensure_role "$ECS_EXECUTION_ROLE_NAME" "$trust" \
    "ECS pulls the image and creates the log group with this"
  aws iam put-role-policy --role-name "$ECS_EXECUTION_ROLE_NAME" \
    --policy-name "eval-runner-execution" --policy-document "$exec_perms" >/dev/null

  ensure_role "$ECS_TASK_ROLE_NAME" "$trust" \
    "The eval sweep running inside the container uses this"
  aws iam put-role-policy --role-name "$ECS_TASK_ROLE_NAME" \
    --policy-name "eval-runner-task" --policy-document "$task_perms" >/dev/null
  echo "  roles: $ECS_EXECUTION_ROLE_NAME (execution), $ECS_TASK_ROLE_NAME (task)"

  # A Fargate cluster is free and holds no capacity of its own -- it is a
  # namespace for tasks, not a set of machines.
  aws ecs create-cluster --cluster-name "$ECS_CLUSTER_NAME" >/dev/null
  echo "  cluster: $ECS_CLUSTER_NAME"

  # Created here, with retention, rather than letting the awslogs driver create
  # it: a log group created by the driver never expires.
  aws logs create-log-group --log-group-name "/ecs/$ECS_TASK_FAMILY" 2>/dev/null || true
  aws logs put-retention-policy --log-group-name "/ecs/$ECS_TASK_FAMILY" \
    --retention-in-days "$LOG_RETENTION_DAYS" >/dev/null
  echo "  log group: /ecs/$ECS_TASK_FAMILY (${LOG_RETENTION_DAYS}d retention)"

  task_def="$(render_policy "$account" "$ROOT/infra/ecs-task-definition.json")"
  echo "$task_def" | jq empty || die "the rendered task definition is not valid JSON"
  revision="$(aws ecs register-task-definition --cli-input-json "$task_def" \
    --query 'taskDefinition.revision' --output text)" || die "register-task-definition failed"
  green "  registered $ECS_TASK_FAMILY:$revision"
}

network_config() {
  # Fargate requires awsvpc networking, which requires subnets and a security
  # group. The default VPC is used unless overridden, and the task gets a
  # public IP rather than routing through a NAT gateway. That is a cost
  # decision with a real number behind it: the task's only egress is to
  # Bedrock, DynamoDB, CloudWatch and ECR, and a NAT gateway bills about $32 a
  # month whether or not a sweep ever runs. A public IP on a task with no
  # inbound rules costs nothing. VPC endpoints would be the production answer.
  local subnets sg vpc
  subnets="${ECS_SUBNET_IDS:-}"
  sg="${ECS_SECURITY_GROUP_ID:-}"

  if [ -z "$subnets" ]; then
    subnets="$(aws ec2 describe-subnets --filters "Name=default-for-az,Values=true" \
      --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
    [ -n "$subnets" ] || die "no default subnets found; set ECS_SUBNET_IDS=subnet-a,subnet-b"
  fi
  if [ -z "$sg" ]; then
    vpc="$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
      --query 'Vpcs[0].VpcId' --output text)"
    [ "$vpc" != "None" ] || die "no default VPC found; set ECS_SECURITY_GROUP_ID"
    sg="$(aws ec2 describe-security-groups \
      --filters "Name=vpc-id,Values=$vpc" "Name=group-name,Values=default" \
      --query 'SecurityGroups[0].GroupId' --output text)"
  fi
  echo "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=ENABLED}"
}

step_evalrun() {
  blue "== evalrun =="
  local net task_arn task_id exit_code
  net="$(network_config)"
  echo "  $net"

  task_arn="$(aws ecs run-task --cluster "$ECS_CLUSTER_NAME" \
    --task-definition "$ECS_TASK_FAMILY" --launch-type FARGATE \
    --network-configuration "$net" \
    --query 'tasks[0].taskArn' --output text)" || die "run-task failed"
  [ "$task_arn" != "None" ] || die "run-task started nothing; check 'failures' in its response"

  task_id="${task_arn##*/}"
  green "  task $task_id started"
  echo "  waiting for it to stop (a full sweep is 15-25 minutes)..."
  # The ECS waiter gives up after 100 polls at 6s, which is 10 minutes, so it
  # is called in a loop. A sweep outliving one waiter is the normal case here,
  # and that is the same 15-minute-class duration that ruled out Lambda.
  until aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER_NAME" --tasks "$task_arn" 2>/dev/null
  do
    echo "  still running..."
  done

  exit_code="$(aws ecs describe-tasks --cluster "$ECS_CLUSTER_NAME" --tasks "$task_arn" \
    --query 'tasks[0].containers[0].exitCode' --output text)"
  echo
  blue "  logs (/ecs/$ECS_TASK_FAMILY, stream eval/eval-runner/$task_id):"
  aws logs tail "/ecs/$ECS_TASK_FAMILY" --since 1h --format short 2>/dev/null | tail -40 \
    || warn "  no logs yet"
  echo
  if [ "$exit_code" = "0" ]; then
    green "  task exited 0"
    echo "  Per-case results are in DynamoDB under PK=RUN#<run-id>; the aggregate"
    echo "  scores are CloudWatch metrics in $EVAL_METRICS_NAMESPACE, which is what"
    echo "  the two quality alarms read."
  else
    die "task exited $exit_code -- see the logs above"
  fi
}

# --------------------------------------------------------------------------
# 15-17. Bedrock AgentCore Runtime
#
# A second deployment target for the same agent, not a replacement for the
# Lambda. AgentCore runs a container and speaks HTTP to it rather than invoking
# a function, so the only new code is the transport in agent/server.py -- auth,
# validation, the Converse loop and the five tools are the same modules.
# See docs/decisions.md ADR-5.
# --------------------------------------------------------------------------

require_agentcore_cli() {
  # These APIs are recent enough that an older AWS CLI v2 does not have the
  # commands at all. Failing here, with the reason, beats a bare
  # "Invalid choice: 'bedrock-agentcore-control'" twenty lines into a deploy.
  aws bedrock-agentcore-control help >/dev/null 2>&1 || die \
    "this AWS CLI has no 'bedrock-agentcore-control' commands -- upgrade it and re-run.
  Nothing else in this script needs them: the Lambda deployment and the ECS
  eval runner are unaffected."
}

step_agentcore_image() {
  blue "== agentcore image =="
  require_docker
  local account registry uri
  account="$(account_id)"
  registry="$(ecr_registry "$account")"
  uri="$registry/$AGENTCORE_REPO_NAME:$IMAGE_TAG"

  ensure_ecr_repo "$AGENTCORE_REPO_NAME"
  docker_login "$registry"

  # AgentCore accepts linux/arm64 only, and rejects an amd64 image at create
  # time with an error that does not mention architecture. On an x86 host this
  # needs buildx with QEMU emulation, which is why the platform is explicit and
  # why this build is slower than the eval runner's.
  echo "  building $uri for linux/arm64"
  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --platform linux/arm64 --load \
      -f "$ROOT/infra/Dockerfile.agentcore" -t "$uri" "$ROOT" || die "buildx build failed"
  else
    warn "  docker buildx is not available; falling back to a plain build."
    warn "  On an x86 host that produces an amd64 image, which AgentCore rejects."
    docker build --platform linux/arm64 \
      -f "$ROOT/infra/Dockerfile.agentcore" -t "$uri" "$ROOT" || die "docker build failed"
  fi

  echo "  pushing"
  docker push "$uri" >/dev/null || die "docker push failed"
  green "  pushed $uri"
}

agentcore_runtime_field() {
  # One place that reads a field off the runtime by name, so every caller
  # tolerates it not existing yet.
  aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$AGENTCORE_RUNTIME_NAME'].$1 | [0]" \
    --output text 2>/dev/null || echo None
}

step_agentcore_deploy() {
  blue "== agentcore deploy =="
  require_agentcore_cli
  local account registry uri trust perms role_arn runtime_id env_json
  account="$(account_id)"
  registry="$(ecr_registry "$account")"
  uri="$registry/$AGENTCORE_REPO_NAME:$IMAGE_TAG"

  trust="$(render_policy "$account" "$POLICY_DIR/agentcore-trust-policy.json")"
  perms="$(render_policy "$account" "$POLICY_DIR/agentcore-runtime-role.json")"
  ensure_role "$AGENTCORE_ROLE_NAME" "$trust" \
    "Execution role for the triage agent on AgentCore Runtime"
  aws iam put-role-policy --role-name "$AGENTCORE_ROLE_NAME" \
    --policy-name "agentcore-runtime" --policy-document "$perms" >/dev/null
  role_arn="arn:aws:iam::$account:role/$AGENTCORE_ROLE_NAME"
  echo "  role: $role_arn"

  # The same environment the Lambda gets, so the two deployments are one agent
  # configured identically rather than two agents that merely resemble one
  # another. Built with jq so a value containing a comma cannot corrupt it.
  env_json="$(jq -n \
    --arg table "$TABLE_NAME" \
    --arg topic "arn:aws:sns:$AWS_REGION:$account:$TOPIC_NAME" \
    --arg model "$MODEL_ID" \
    --arg variant "$PROMPT_VARIANT" \
    --arg ns "$AGENT_METRICS_NAMESPACE" \
    --arg svc_ns "$SERVICE_METRICS_NAMESPACE" \
    '{TRIAGE_TABLE_NAME:$table, TRIAGE_SNS_TOPIC_ARN:$topic, TRIAGE_MODEL_ID:$model,
      TRIAGE_PROMPT_VARIANT:$variant, TRIAGE_METRICS_NAMESPACE:$ns,
      TRIAGE_SERVICE_METRICS_NAMESPACE:$svc_ns, TRIAGE_LOG_LEVEL:"INFO"}')"

  runtime_id="$(agentcore_runtime_field agentRuntimeId)"

  if [ "$runtime_id" = "None" ] || [ -z "$runtime_id" ]; then
    echo "  creating runtime $AGENTCORE_RUNTIME_NAME"
    aws bedrock-agentcore-control create-agent-runtime \
      --agent-runtime-name "$AGENTCORE_RUNTIME_NAME" \
      --agent-runtime-artifact "containerConfiguration={containerUri=$uri}" \
      --network-configuration "networkMode=PUBLIC" \
      --protocol-configuration "serverProtocol=HTTP" \
      --role-arn "$role_arn" \
      --environment-variables "$env_json" \
      --description "On-call triage agent -- same code as the triage-agent Lambda" \
      >/dev/null || die "create-agent-runtime failed"
  else
    echo "  runtime exists ($runtime_id); updating it to $IMAGE_TAG"
    aws bedrock-agentcore-control update-agent-runtime \
      --agent-runtime-id "$runtime_id" \
      --agent-runtime-artifact "containerConfiguration={containerUri=$uri}" \
      --network-configuration "networkMode=PUBLIC" \
      --protocol-configuration "serverProtocol=HTTP" \
      --role-arn "$role_arn" \
      --environment-variables "$env_json" \
      >/dev/null || die "update-agent-runtime failed"
  fi

  green "  runtime arn: $(agentcore_runtime_field agentRuntimeArn)"
  warn "  Inbound auth is IAM SigV4: a caller needs bedrock-agentcore:InvokeAgentRuntime."
  warn "  There is no public URL, which is the practical difference from the API"
  warn "  Gateway deployment -- and the reason that one needs an API key and this"
  warn "  one does not."
}

step_agentcore_invoke() {
  blue "== agentcore invoke =="
  require_agentcore_cli
  local arn session out payload
  arn="$(agentcore_runtime_field agentRuntimeArn)"
  [ "$arn" != "None" ] || die "no runtime named $AGENTCORE_RUNTIME_NAME; run the deploy step"
  echo "  runtime: $arn"

  # AgentCore rejects a session id shorter than 33 characters.
  session="triage-smoke-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  payload='{"alarm_name":"checkout-api-5xx-high","service":"checkout-api","environment":"prod","metric":"Error5xxRate","value":8.4,"threshold":1.0,"duration_min":14}'
  out="$(mktemp)"

  aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "$arn" \
    --runtime-session-id "$session" \
    --content-type "application/json" \
    --payload "$payload" \
    "$out" >/dev/null || die "invoke-agent-runtime failed"

  jq . < "$out" 2>/dev/null || cat "$out"
  rm -f "$out"
  echo
  blue "  recent runtime logs:"
  aws logs tail "/aws/bedrock-agentcore/runtimes" --since 10m --format short 2>/dev/null \
    | tail -20 || warn "  no logs yet (the group appears after the first invocation)"
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

usage() {
  cat <<EOF
Usage: bash infra/setup.sh <step>

The serverless system -- steps in order, and what \`all\` runs:
  preflight   credentials, tooling, and Bedrock model access
  iam         execution role + tightly scoped inline policy
  sns         paging topic (set PAGER_EMAIL to subscribe)
  dynamodb    single table, on-demand, TTL enabled
  package     build function.zip (agent/ only -- boto3 ships with the runtime)
  lambda      create or update the function
  api         HTTP API with POST /alert
  seed        runbooks, deploy history, synthetic CloudWatch metrics
  alarms      2 operational + 2 agent-quality alarms
  smoke       POST a real alert and tail the logs

  all         the ten steps above, in order

The ECS Fargate eval runner -- a sweep outlives Lambda's 15-minute ceiling:
  ecr         two ECR repositories, scan-on-push, untagged images expire
  image       build and push the eval-runner image (amd64)
  ecs         execution role + task role + cluster + log group + task definition
  evalrun     run one full sweep as a Fargate task and tail it to completion

  ecs_all     ecr, image, ecs -- everything except actually running a sweep

Bedrock AgentCore Runtime -- the same agent, deployed a second way:
  agentcore_image    build and push the agent image (arm64; AgentCore requires it)
  agentcore_deploy   runtime execution role + create or update the agent runtime
  agentcore_invoke   invoke it once with a real alert and print the response

  agentcore_all      all three of the above, in order

Both container targets need Docker and are deliberately outside \`all\`: neither
is required to run or demonstrate the serverless system, and both push images
that cost storage. Run them explicitly.

Configuration (override in the environment or infra/.env -- see .env.example):
  AWS_REGION=$AWS_REGION
  FUNCTION_NAME=$FUNCTION_NAME
  TABLE_NAME=$TABLE_NAME
  TOPIC_NAME=$TOPIC_NAME
  MODEL_ID=$MODEL_ID
  PROMPT_VARIANT=$PROMPT_VARIANT
  ECR_REPO_NAME=$ECR_REPO_NAME
  ECS_CLUSTER_NAME=$ECS_CLUSTER_NAME
  AGENTCORE_RUNTIME_NAME=$AGENTCORE_RUNTIME_NAME
  IMAGE_TAG=$IMAGE_TAG
  PAGER_EMAIL=${PAGER_EMAIL:-<unset>}
  TRIAGE_API_KEY=${TRIAGE_API_KEY:+<set>}

Teardown: bash infra/teardown.sh all
EOF
}

main() {
  local step="${1:-}"
  shift || true
  case "$step" in
    preflight|iam|sns|dynamodb|package|lambda|api|seed|alarms|smoke|ecr|image|ecs|evalrun|agentcore_image|agentcore_deploy|agentcore_invoke)
      "step_$step" "$@" ;;
    all)
      step_preflight; step_iam; step_sns; step_dynamodb
      step_package;   step_lambda; step_api; step_seed; step_alarms; step_smoke ;;
    ecs_all)
      step_ecr; step_image; step_ecs ;;
    agentcore_all)
      step_agentcore_image; step_agentcore_deploy; step_agentcore_invoke ;;
    ""|-h|--help|help) usage ;;
    *) die "unknown step '$step'. Run without arguments to see the list." ;;
  esac
}

main "$@"
