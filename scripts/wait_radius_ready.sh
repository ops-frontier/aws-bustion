#!/usr/bin/env bash
set -euo pipefail

: "${RADIUS_INSTANCE_ID:?RADIUS_INSTANCE_ID is required}"
: "${AWS_REGION:?AWS_REGION is required}"

deadline=$((SECONDS + 1200))
while true; do
  ping=$(aws ssm describe-instance-information \
    --profile terraform-role --region "$AWS_REGION" \
    --filters "Key=InstanceIds,Values=$RADIUS_INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text)
  if [[ "$ping" == Online ]]; then
    break
  fi
  if ((SECONDS >= deadline)); then
    echo "RADIUS instance did not become available in SSM within 20 minutes." >&2
    exit 1
  fi
  sleep 10
done

command_id=$(aws ssm send-command \
  --profile terraform-role --region "$AWS_REGION" \
  --instance-ids "$RADIUS_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["set -eu","cloud-init status --wait","systemctl is-active --quiet freeradius","freeradius -C"],"executionTimeout":["1200"]}' \
  --query Command.CommandId --output text)
echo "Waiting for RADIUS readiness: $RADIUS_INSTANCE_ID ($command_id)"

while ((SECONDS < deadline)); do
  status=$(aws ssm list-command-invocations \
    --profile terraform-role --region "$AWS_REGION" \
    --command-id "$command_id" --instance-id "$RADIUS_INSTANCE_ID" \
    --query 'CommandInvocations[0].Status' --output text)
  case "$status" in
    Success)
      echo "RADIUS bootstrap and service checks succeeded."
      exit 0
      ;;
    None|Pending|InProgress|Delayed)
      sleep 10
      ;;
    *)
      echo "RADIUS readiness failed: $status" >&2
      aws ssm get-command-invocation \
        --profile terraform-role --region "$AWS_REGION" \
        --command-id "$command_id" --instance-id "$RADIUS_INSTANCE_ID" \
        --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
      exit 1
      ;;
  esac
done

echo "RADIUS readiness timed out after 20 minutes." >&2
aws ssm cancel-command --profile terraform-role --region "$AWS_REGION" \
  --command-id "$command_id" --instance-ids "$RADIUS_INSTANCE_ID"
exit 1
