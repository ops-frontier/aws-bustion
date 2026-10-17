import configparser
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import login


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.aws = Path(self.home.name) / ".aws"
        self.aws.mkdir()
        self.config = self.aws / "config"
        self.config.write_text(
            "[profile terraform-mfa]\n"
            "role_arn = arn:aws:iam::123456789012:role/test\n"
            "source_profile = base\n"
            "mfa_serial = arn:aws:iam::123456789012:mfa/test\n"
            "[profile terraform-role]\nregion = ap-northeast-1\n"
        )
        self.path = self.aws / "credentials"
        self.original = (
            "[base]\naws_access_key_id = base-key\naws_secret_access_key = base-secret\n"
            "[other]\naws_access_key_id = other-key\n"
            "[terraform-role]\naws_session_token = old-token\n"
        )
        self.path.write_text(self.original)
        self.env = patch.dict(os.environ, {
            "AWS_CONFIG_FILE": str(self.config),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.path),
            "AWS_ACCESS_KEY_ID": "environment-key",
            "AWS_SECRET_ACCESS_KEY": "environment-secret",
            "AWS_SESSION_TOKEN": "environment-token",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.credentials = {
            "AccessKeyId": "session-key",
            "SecretAccessKey": "session-secret",
            "SessionToken": "session-token",
            "Expiration": "2099-01-01T00:00:00Z",
        }
        self.output = io.StringIO()
        self.error = io.StringIO()

    def run_login(self, token="012345", failure=None):
        with (
            patch("builtins.input", return_value=token) as prompt,
            patch("login.subprocess.run") as run,
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.error),
        ):
            if failure is not None:
                run.side_effect = failure
            else:
                run.return_value = subprocess.CompletedProcess(
                    [], 0, json.dumps({"Credentials": self.credentials}), ""
                )
            status = login.main()
        return status, run, prompt

    def test_login_saves_all_session_fields_and_preserves_other_profiles(self):
        status, run, prompt = self.run_login()
        self.assertEqual(status, 0, self.error.getvalue())
        prompt.assert_called_once_with("MFA OTP (6 digits): ")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--profile") + 1], "base")
        self.assertEqual(command[command.index("--token-code") + 1], "012345")
        self.assertEqual(
            command[command.index("--role-arn") + 1],
            "arn:aws:iam::123456789012:role/test",
        )
        self.assertEqual(
            command[command.index("--serial-number") + 1],
            "arn:aws:iam::123456789012:mfa/test",
        )
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            self.assertNotIn(name, run.call_args.kwargs["env"])
        saved = configparser.ConfigParser()
        saved.read(self.path)
        self.assertEqual(saved["base"]["aws_access_key_id"], "base-key")
        self.assertEqual(saved["other"]["aws_access_key_id"], "other-key")
        self.assertEqual(dict(saved["terraform-role"]), {
            "aws_access_key_id": "session-key",
            "aws_secret_access_key": "session-secret",
            "aws_session_token": "session-token",
        })
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("2099-01-01", self.output.getvalue())
        for value in ("session-key", "session-secret", "session-token", "012345"):
            self.assertNotIn(value, self.output.getvalue() + self.error.getvalue())
        self.assertEqual(list(self.aws.glob(".credentials-*")), [])

    def test_invalid_otp_does_not_call_aws_or_modify_credentials(self):
        for token in ("", "12345", "1234567", "abcdef", "１２３４５６"):
            with self.subTest(token=token):
                status, run, _ = self.run_login(token)
                self.assertEqual(status, 1)
                run.assert_not_called()
                self.assertEqual(self.path.read_text(), self.original)

    def test_aws_failure_is_reported_without_modifying_credentials(self):
        status, _, _ = self.run_login(failure=subprocess.CalledProcessError(
            255, ["aws"], stderr="AccessDenied: invalid MFA code"
        ))
        self.assertEqual(status, 1)
        self.assertIn("AccessDenied", self.error.getvalue())
        self.assertEqual(self.path.read_text(), self.original)

    def test_post_create_configuration_can_be_used_for_login(self):
        script = Path(__file__).resolve().parents[1] / ".devcontainer/post-create.sh"
        result = subprocess.run(
            ["bash", str(script)],
            env={
                **os.environ,
                "HOME": self.home.name,
                "AWS_ACCESS_KEY_ID": "base-key",
                "AWS_SECRET_ACCESS_KEY": "base-secret",
                "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/test",
                "AWS_SERIAL_NUMBER": "arn:aws:iam::123456789012:mfa/test",
                "BUSTION_GITHUB_ORGANIZATION": "example-org",
                "APPSTREAM_IMAGE_NAME": "test-image",
            },
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status, _, _ = self.run_login()
        self.assertEqual(status, 0, self.error.getvalue())
        saved = configparser.ConfigParser()
        saved.read(self.path)
        self.assertEqual(saved["base"]["aws_access_key_id"], "base-key")
        self.assertEqual(saved["terraform-role"]["aws_session_token"], "session-token")
        config = configparser.ConfigParser()
        config.read(self.config)
        self.assertNotIn("role_arn", config["profile terraform-role"])
        provider = (script.parents[1] / "terraform/versions.tf").read_text()
        self.assertIn('profile                  = "terraform-role"', provider)

    def test_malformed_json_does_not_modify_credentials(self):
        with (
            patch("builtins.input", return_value="012345"),
            patch("login.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, "not-json", ""
            )),
            contextlib.redirect_stderr(self.error),
        ):
            self.assertEqual(login.main(), 1)
        self.assertEqual(self.path.read_text(), self.original)

    def test_missing_aws_cli_is_reported(self):
        status, _, _ = self.run_login(failure=FileNotFoundError("aws not found"))
        self.assertEqual(status, 1)
        self.assertIn("aws not found", self.error.getvalue())

    def test_invalid_response_does_not_modify_credentials(self):
        for credentials in (
            {}, {"AccessKeyId": "only-key"}, None,
            {**self.credentials, "Expiration": "2000-01-01T00:00:00Z"},
            {**self.credentials, "Expiration": "not-a-date"},
            {**self.credentials, "SessionToken": ""},
        ):
            with self.subTest(credentials=credentials):
                self.credentials = credentials
                status, _, _ = self.run_login()
                self.assertEqual(status, 1)
                self.assertEqual(self.path.read_text(), self.original)

    def test_missing_config_and_invalid_role_fail_before_prompt(self):
        for text in ("", "[profile terraform-mfa]\nrole_arn = role-name\n"):
            with self.subTest(text=text):
                self.config.write_text(text)
                status, run, prompt = self.run_login()
                self.assertEqual(status, 1)
                run.assert_not_called()
                prompt.assert_not_called()

    def test_cancelled_input_does_not_modify_credentials(self):
        with patch("builtins.input", side_effect=EOFError), contextlib.redirect_stderr(self.error):
            self.assertEqual(login.main(), 1)
        self.assertEqual(self.path.read_text(), self.original)

    def test_failed_atomic_replace_preserves_credentials_and_cleans_temporary_file(self):
        with patch.object(Path, "replace", side_effect=OSError("write denied")):
            status, _, _ = self.run_login()
        self.assertEqual(status, 1)
        self.assertIn("write denied", self.error.getvalue())
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(list(self.aws.glob(".credentials-*")), [])


if __name__ == "__main__":
    unittest.main()
