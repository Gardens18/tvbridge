#!/bin/bash
# Run a PowerShell command on the Hantec pilot server through AWS Systems Manager (HTTPS only,
# no SSH needed). Needs AWS credentials with SSM permissions (the claude-cloud user has them).
#   ops/ssm.sh 'C:\tvbridge-setup\status.ps1'
#   ops/ssm.sh 'Get-Content C:\tvbridge-home\engine.out -Tail 40'
#   ops/ssm.sh 'Set-Content C:\tvbridge-setup\jobs\x.job.ps1 "..."'   # a GUI job for the desktop runner
set -euo pipefail
INSTANCE="${TVBRIDGE_INSTANCE:-i-0303e091d02e9ed48}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eu-north-1}"
CMD="$1"
ID=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunPowerShellScript \
      --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]]}))' "$CMD")" \
      --query Command.CommandId --output text)
for _ in $(seq 1 60); do
  sleep 2
  STATUS=$(aws ssm get-command-invocation --command-id "$ID" --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Pending|InProgress|Delayed) continue;; esac
  break
done
aws ssm get-command-invocation --command-id "$ID" --instance-id "$INSTANCE" --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
