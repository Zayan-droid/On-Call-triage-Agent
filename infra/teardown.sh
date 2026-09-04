#!/usr/bin/env bash
#
# Delete everything setup.sh created.
#
#   bash infra/teardown.sh              # show what would be deleted
#   bash infra/teardown.sh all          # delete it, after one confirmation
#   bash infra/teardown.sh metrics      # just the custom metrics explanation
#
# Run this when you stop working on the project. Two things here bill while
# idle and neither is obvious:
#
#   * Custom CloudWatch metrics: ~$0.30 per metric per month, prorated hourly.
#     Seeding all 38 cases creates roughly 42 series, so about $12/month if
#     left alone. They cannot be deleted -- they expire 15 months after their
#     last datapoint. Not publishing more is the only lever.
#   * The DynamoDB table: on-demand costs nothing to keep idle beyond storage,
#     but it holds the seeded world and every eval run.
#
# Deletes are ordered so nothing is left referencing something already gone.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/infra/.env" ] && . "$ROOT/infra/.env"

export AWS_REGION="${AWS_REGION:-us-east-1}"
FUNCTION_NAME="${FUNCTION_NAME:-triage-agent}"
ROLE_NAME="${ROLE_NAME:-triage-agent-role}"
POLICY_NAME="${POLICY_NAME:-triage-agent-permissions}"
TABLE_NAME="${TABLE_NAME:-triage-agent}"
TOPIC_NAME="${TOPIC_NAME:-triage-oncall-pages}"
API_NAME="${API_NAME:-triage-agent-api}"

# The container deployment targets. Same defaults as setup.sh.
ECR_REPO_NAME="${ECR_REPO_NAME:-triage-eval-runner}"
ECS_TASK_FAMILY="${ECS_TASK_FAMILY:-triage-eval-runner}"
ECS_CLUSTER_NAME="${ECS_CLUSTER_NAME:-triage-eval}"
ECS_EXECUTION_ROLE_NAME="${ECS_EXECUTION_ROLE_NAME:-triage-eval-runner-execution-role}"
ECS_TASK_ROLE_NAME="${ECS_TASK_ROLE_NAME:-triage-eval-runner-task-role}"
AGENTCORE_REPO_NAME="${AGENTCORE_REPO_NAME:-triage-agentcore}"
AGENTCORE_RUNTIME_NAME="${AGENTCORE_RUNTIME_NAME:-triage_agent}"
AGENTCORE_ROLE_NAME="${AGENTCORE_ROLE_NAME:-triage-agentcore-role}"

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*" >&2; }

gone() { echo "  (already gone)"; }

step_api() {
  blue "== api =="
  local id
  id="$(aws apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text 2>/dev/null || echo None)"
  if [ "$id" = "None" ] || [ -z "$id" ]; then gone; return; fi
  aws apigatewayv2 delete-api --api-id "$id" && green "  deleted api $id"
}

step_lambda() {
  blue "== lambda =="
  aws lambda delete-function --function-name "$FUNCTION_NAME" 2>/dev/null \
    && green "  deleted function $FUNCTION_NAME" || gone
  aws logs delete-log-group --log-group-name "/aws/lambda/$FUNCTION_NAME" 2>/dev/null \
    && green "  deleted log group" || echo "  (no log group)"
}

step_alarms() {
  blue "== alarms =="
  local names
  names="$(aws cloudwatch describe-alarms --alarm-name-prefix "triage-" \
    --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null || true)"
  if [ -z "$names" ]; then gone; return; fi
  # shellcheck disable=SC2086
  aws cloudwatch delete-alarms --alarm-names $names && green "  deleted: $names"
}

step_dynamodb() {
  blue "== dynamodb =="
  aws dynamodb delete-table --table-name "$TABLE_NAME" >/dev/null 2>&1 \
    && green "  deleting table $TABLE_NAME (this takes a moment)" || gone
}

step_sns() {
  blue "== sns =="
  local account arn
  account="$(aws sts get-caller-identity --query Account --output text)"
  arn="arn:aws:sns:$AWS_REGION:$account:$TOPIC_NAME"
  # Deleting the topic removes its subscriptions with it.
  aws sns delete-topic --topic-arn "$arn" 2>/dev/null \
    && green "  deleted topic $TOPIC_NAME" || gone
}

step_iam() {
  blue "== iam =="
  delete_role "$ROLE_NAME"
}

step_metrics() {
  blue "== custom metrics =="
  warn "  CloudWatch custom metrics cannot be deleted. They stop being billed"
  warn "  once nothing publishes to them, and disappear 15 months after their"
  warn "  last datapoint. Namespaces this project writes to:"
  echo "    OncallTriage           (agent, via EMF)"
  echo "    OncallTriage/Services  (the synthetic fleet, via seed_data.py)"
  echo "    OncallTriage/Eval      (harness scores)"
  echo
  echo "  Current series count:"
  for namespace in OncallTriage OncallTriage/Services OncallTriage/Eval; do
    local count
    count="$(aws cloudwatch list-metrics --namespace "$namespace" \
      --query 'length(Metrics)' --output text 2>/dev/null || echo 0)"
    printf '    %-24s %s\n' "$namespace" "$count"
  done
}

delete_role() {
  # A role cannot be deleted while it still has inline policies attached, and
  # the error for that says "DeleteConflict" without naming the policy. Detach
  # everything first, whatever it is called.
  local name="$1" policies
  aws iam get-role --role-name "$name" >/dev/null 2>&1 || { echo "  (no role $name)"; return; }
  policies="$(aws iam list-role-policies --role-name "$name" \
    --query 'PolicyNames[]' --output text 2>/dev/null || true)"
  for policy in $policies; do
    aws iam delete-role-policy --role-name "$name" --policy-name "$policy" >/dev/null
    echo "  deleted inline policy $policy from $name"
  done
  aws iam delete-role --role-name "$name" >/dev/null 2>&1 \
    && green "  deleted role $name" || warn "  could not delete role $name"
}

step_agentcore() {
  blue "== agentcore =="
  local id
  if ! aws bedrock-agentcore-control help >/dev/null 2>&1; then
    warn "  this AWS CLI has no bedrock-agentcore-control commands; skipping the"
    warn "  runtime. Delete it from the console if one exists."
  else
    id="$(aws bedrock-agentcore-control list-agent-runtimes \
      --query "agentRuntimes[?agentRuntimeName=='$AGENTCORE_RUNTIME_NAME'].agentRuntimeId | [0]" \
      --output text 2>/dev/null || echo None)"
    if [ "$id" = "None" ] || [ -z "$id" ]; then
      gone
    else
      aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$id" >/dev/null \
        && green "  deleted runtime $AGENTCORE_RUNTIME_NAME ($id)" \
        || warn "  could not delete runtime $id"
    fi
  fi
  delete_role "$AGENTCORE_ROLE_NAME"
}

step_ecs() {
  blue "== ecs =="
  local revisions

  # Task definitions are versioned and never really deleted -- deregistering
  # marks a revision INACTIVE, and AWS keeps it. That is not a leak: an
  # inactive revision is free, and there is no API that removes it.
  revisions="$(aws ecs list-task-definitions --family-prefix "$ECS_TASK_FAMILY" \
    --status ACTIVE --query 'taskDefinitionArns[]' --output text 2>/dev/null || true)"
  for arn in $revisions; do
    aws ecs deregister-task-definition --task-definition "$arn" >/dev/null 2>&1 \
      && echo "  deregistered ${arn##*/}"
  done

  # A cluster refuses to delete while tasks are still running in it.
  local running
  running="$(aws ecs list-tasks --cluster "$ECS_CLUSTER_NAME" \
    --query 'taskArns[]' --output text 2>/dev/null || true)"
  for task in $running; do
    aws ecs stop-task --cluster "$ECS_CLUSTER_NAME" --task "$task" \
      --reason "teardown" >/dev/null 2>&1 && warn "  stopped running task ${task##*/}"
  done

  aws ecs delete-cluster --cluster "$ECS_CLUSTER_NAME" >/dev/null 2>&1 \
    && green "  deleted cluster $ECS_CLUSTER_NAME" || gone

  aws logs delete-log-group --log-group-name "/ecs/$ECS_TASK_FAMILY" 2>/dev/null \
    && green "  deleted log group /ecs/$ECS_TASK_FAMILY" || echo "  (no log group)"

  delete_role "$ECS_EXECUTION_ROLE_NAME"
  delete_role "$ECS_TASK_ROLE_NAME"
}

step_ecr() {
  blue "== ecr =="
  # --force because a repository holding images refuses to delete without it,
  # and the images are the thing being billed.
  for repo in "$ECR_REPO_NAME" "$AGENTCORE_REPO_NAME"; do
    aws ecr delete-repository --repository-name "$repo" --force >/dev/null 2>&1 \
      && green "  deleted repository $repo (and its images)" \
      || echo "  (no repository $repo)"
  done
}

step_local() {
  blue "== local build artifacts =="
  rm -rf "$ROOT/build" && green "  removed build/"
}

usage() {
  cat <<EOF
Usage: bash infra/teardown.sh <step|all>

Steps (safe to run individually, in any order):
  api dynamodb sns iam lambda alarms local
  agentcore  delete the AgentCore runtime and its execution role
  ecs        deregister task definitions, delete the cluster, log group, roles
  ecr        delete both image repositories and everything in them
  metrics    explain the one thing that cannot be deleted

  all        everything above, in dependency order, after one confirmation

Targets in $AWS_REGION:
  function : $FUNCTION_NAME
  role     : $ROLE_NAME
  table    : $TABLE_NAME
  topic    : $TOPIC_NAME
  api      : $API_NAME
  cluster  : $ECS_CLUSTER_NAME
  runtime  : $AGENTCORE_RUNTIME_NAME
  repos    : $ECR_REPO_NAME, $AGENTCORE_REPO_NAME
EOF
}

main() {
  case "${1:-}" in
    api|lambda|alarms|dynamodb|sns|iam|metrics|local|agentcore|ecs|ecr) "step_${1}" ;;
    all)
      usage
      echo
      warn "This deletes the resources listed above, including the DynamoDB table"
      warn "and everything in it (seeded world, incidents, eval run history), and"
      warn "both container images."
      read -r -p "Type the table name '$TABLE_NAME' to confirm: " answer
      [ "$answer" = "$TABLE_NAME" ] || { echo "Aborted."; exit 1; }
      echo
      # Ordered so nothing is left referencing something already gone: the
      # AgentCore runtime and the ECS task definition both point at images, so
      # they go before the repositories that hold them.
      step_api; step_lambda; step_alarms; step_agentcore; step_ecs; step_ecr
      step_dynamodb; step_sns; step_iam; step_local
      echo
      step_metrics
      green "Teardown complete."
      ;;
    ""|-h|--help|help) usage ;;
    *) echo "unknown step '${1}'"; usage; exit 1 ;;
  esac
}

main "$@"
