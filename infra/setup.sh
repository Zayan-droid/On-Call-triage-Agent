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
      -e "s|\${ECS_TASK_FAMILY}|${ECS_TASK_FAMILY:-triage-eval-runner}|g" \
      -e "s|\${ECR_REPO_NAME}|${ECR_REPO_NAME:-triage-eval-runner}|g" \
      "$file"
}

step_iam() {
  blue "== iam =="
  local account trust perms
  account="$(account_id)"
  trust="$(render_policy "$account" "$POLICY_DIR/lambda-trust-policy.json")"
  perms="$(render_policy "$account" "$POLICY_DIR/triage-agent-permissions.json")"

  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "  role $ROLE_NAME exists; updating its trust policy"
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
      --policy-document "$trust" >/dev/null
  else
    echo "  creating role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" \
      --assume-role-policy-document "$trust" \
      --description "Execution role for the on-call triage agent Lambda" >/dev/null
  fi

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
# Dispatch
# --------------------------------------------------------------------------

usage() {
  cat <<EOF
Usage: bash infra/setup.sh <step>

Steps, in order:
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

  all         every step above, in order

Configuration (override in the environment or infra/.env):
  AWS_REGION=$AWS_REGION
  FUNCTION_NAME=$FUNCTION_NAME
  TABLE_NAME=$TABLE_NAME
  TOPIC_NAME=$TOPIC_NAME
  MODEL_ID=$MODEL_ID
  PAGER_EMAIL=${PAGER_EMAIL:-<unset>}
  TRIAGE_API_KEY=${TRIAGE_API_KEY:+<set>}

Teardown: bash infra/teardown.sh all
EOF
}

main() {
  local step="${1:-}"
  shift || true
  case "$step" in
    preflight|iam|sns|dynamodb|package|lambda|api|seed|alarms|smoke)
      "step_$step" "$@" ;;
    all)
      step_preflight; step_iam; step_sns; step_dynamodb
      step_package;   step_lambda; step_api; step_seed; step_alarms; step_smoke ;;
    ""|-h|--help|help) usage ;;
    *) die "unknown step '$step'. Run without arguments to see the list." ;;
  esac
}

main "$@"
