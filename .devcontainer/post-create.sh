#!/usr/bin/env bash
set -euo pipefail

: "${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID in Codespaces secrets}"
: "${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY in Codespaces secrets}"
: "${AWS_ROLE_ARN:?Set AWS_ROLE_ARN in Codespaces secrets}"
: "${AWS_SERIAL_NUMBER:?Set AWS_SERIAL_NUMBER in Codespaces secrets}"
: "${TF_VAR_github_organization:?Set GITHUB_ORGANIZATION in Codespaces secrets}"

mkdir -p "$HOME/.aws"
chmod 700 "$HOME/.aws"
umask 077

cat > "$HOME/.aws/credentials" <<EOF
[base]
aws_access_key_id = ${AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${AWS_SECRET_ACCESS_KEY}
EOF

cat > "$HOME/.aws/config" <<EOF
[profile terraform-role]
role_arn = ${AWS_ROLE_ARN}
source_profile = base
mfa_serial = ${AWS_SERIAL_NUMBER}
region = ap-northeast-1
EOF

chmod 600 "$HOME/.aws/credentials" "$HOME/.aws/config"