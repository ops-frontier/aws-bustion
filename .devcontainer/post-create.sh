#!/usr/bin/env bash
set -euo pipefail

: "${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID in Codespaces secrets}"
: "${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY in Codespaces secrets}"
: "${AWS_ROLE_ARN:?Set AWS_ROLE_ARN in Codespaces secrets}"
: "${AWS_SERIAL_NUMBER:?Set AWS_SERIAL_NUMBER in Codespaces secrets}"
: "${GITHUB_ORGANIZATION:?Set GITHUB_ORGANIZATION in Codespaces secrets}"
: "${APPSTREAM_IMAGE_NAME:?Set APPSTREAM_IMAGE_NAME in Codespaces secrets}"

export TF_VAR_github_organization="$GITHUB_ORGANIZATION"
export TF_VAR_appstream_image_name="$APPSTREAM_IMAGE_NAME"

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

mkdir -p "$HOME/.config/aws-bustion"
printf 'export TF_VAR_github_organization=%q\nexport TF_VAR_appstream_image_name=%q\n' \
  "$TF_VAR_github_organization" "$TF_VAR_appstream_image_name" \
  > "$HOME/.config/aws-bustion/terraform-env.sh"

# postCreateCommand runs in a child process; load exports in future terminals.
source_line='source "$HOME/.config/aws-bustion/terraform-env.sh"'
touch "$HOME/.bashrc"
if ! grep -Fxq "$source_line" "$HOME/.bashrc"; then
  printf '\n%s\n' "$source_line" >> "$HOME/.bashrc"
fi