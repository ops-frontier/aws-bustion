#!/usr/bin/env python3
"""Obtain MFA-backed role credentials for Terraform using the AWS CLI."""

import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def login():
    config_path = Path(os.environ.get("AWS_CONFIG_FILE", Path.home() / ".aws/config"))
    config = configparser.ConfigParser(interpolation=None)
    with config_path.open() as stream:
        config.read_file(stream)
    profile = config["profile terraform-mfa"]
    role_arn = profile["role_arn"]
    if not re.fullmatch(r"arn:[a-z0-9-]+:iam::[0-9]{12}:role/.+", role_arn):
        raise ValueError("terraform-mfa role_arn must be a complete IAM role ARN.")
    serial = profile["mfa_serial"]
    source = profile["source_profile"]
    if not serial or not source:
        raise ValueError("terraform-mfa requires mfa_serial and source_profile.")

    token = input("MFA OTP (6 digits): ").strip()
    if not re.fullmatch(r"[0-9]{6}", token):
        raise ValueError("MFA OTP must contain exactly 6 ASCII digits.")

    env = os.environ.copy()
    # Codespaces exports base credentials; never let them override the profile.
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [
            "aws", "--profile", source, "--no-cli-pager", "sts", "assume-role",
            "--role-arn", role_arn, "--role-session-name", "terraform",
            "--serial-number", serial, "--token-code", token, "--output", "json",
        ],
        env=env, capture_output=True, text=True, check=True,
    )
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or not isinstance(response.get("Credentials"), dict):
        raise ValueError("AWS STS returned no Credentials object.")
    credentials = response["Credentials"]
    fields = {
        "aws_access_key_id": "AccessKeyId",
        "aws_secret_access_key": "SecretAccessKey",
        "aws_session_token": "SessionToken",
    }
    for field in (*fields.values(), "Expiration"):
        if not isinstance(credentials.get(field), str) or not credentials[field].strip():
            raise ValueError(f"AWS STS returned an invalid {field}.")
    expiration = datetime.fromisoformat(credentials["Expiration"].replace("Z", "+00:00"))
    if expiration.tzinfo is None or expiration <= datetime.now(timezone.utc):
        raise ValueError("AWS STS returned an invalid or expired session expiration.")

    path = Path(os.environ.get(
        "AWS_SHARED_CREDENTIALS_FILE", Path.home() / ".aws/credentials"
    ))
    saved = configparser.ConfigParser(interpolation=None)
    if path.exists():
        with path.open() as stream:
            saved.read_file(stream)
    saved["terraform-role"] = {
        name: credentials[field] for name, field in fields.items()
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".credentials-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            saved.write(stream)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Logged in: profile terraform-role (expires {expiration.isoformat()}).")


def main():
    try:
        login()
    except subprocess.CalledProcessError as error:
        print(f"AWS AssumeRole failed:\n{error.stderr.strip()}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, configparser.Error, EOFError) as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
