import configparser
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


DIRECTORY = Path(__file__).resolve().parent
SCRIPT = DIRECTORY / "post-create.sh"
REQUIRED = {
    "AWS_ACCESS_KEY_ID": "test-access-key",
    "AWS_SECRET_ACCESS_KEY": "test-secret-key",
    "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/test",
    "AWS_SERIAL_NUMBER": "arn:aws:iam::123456789012:mfa/test",
    "BUSTION_GITHUB_ORGANIZATION": "example-org",
    "APPSTREAM_IMAGE_NAME": "published-linux-image",
}


class PostCreateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.home.name, **REQUIRED}
        self.bashrc = Path(self.home.name) / ".bashrc"
        self.bashrc.write_text("# Existing terminal settings\nexport EXISTING_SETTING=kept\n")

    def initialize(self, env=None):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
        )

    def terminal_environment(self):
        result = subprocess.run(
            [
                "bash", "--noprofile", "--rcfile", str(self.bashrc), "-ic",
                'printf "%s\\n" "$TF_VAR_github_organization" '
                '"$TF_VAR_appstream_image_name" "$EXISTING_SETTING"',
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.splitlines()

    def test_uppercase_inputs_persist_in_new_terminals(self):
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.terminal_environment(), ["example-org", "published-linux-image", "kept"]
        )
        aws = Path(self.home.name) / ".aws"
        self.assertIn("role_arn = " + REQUIRED["AWS_ROLE_ARN"], (aws / "config").read_text())
        profiles = configparser.ConfigParser()
        profiles.read(aws / "config")
        self.assertEqual(
            dict(profiles["profile terraform-role"]), {"region": "ap-northeast-1"}
        )
        self.assertEqual(profiles["profile terraform-mfa"]["source_profile"], "base")
        self.assertEqual(
            profiles["profile terraform-mfa"]["mfa_serial"], REQUIRED["AWS_SERIAL_NUMBER"]
        )
        credentials = configparser.ConfigParser()
        credentials.read(aws / "credentials")
        self.assertEqual(credentials.sections(), ["base"])
        self.assertEqual((aws / "config").stat().st_mode & 0o777, 0o600)
        self.assertEqual((aws / "credentials").stat().st_mode & 0o777, 0o600)
        config = json.loads((DIRECTORY / "devcontainer.json").read_text())
        self.assertIn("ghcr.io/devcontainers/features/python:1", config["features"])
        for name in ("BUSTION_GITHUB_ORGANIZATION", "APPSTREAM_IMAGE_NAME"):
            self.assertEqual(config["containerEnv"][name], "${localEnv:" + name + "}")
        self.assertNotIn("TF_VAR_github_organization", config["containerEnv"])

    def test_rerun_updates_values_without_duplicate_startup_lines(self):
        self.assertEqual(self.initialize().returncode, 0)
        self.env["APPSTREAM_IMAGE_NAME"] = "updated image 'quoted' $value"
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.terminal_environment(),
            ["example-org", self.env["APPSTREAM_IMAGE_NAME"], "kept"],
        )
        self.assertEqual(self.bashrc.read_text().count('source "$HOME/.config/'), 1)

    def test_missing_inputs_fail_before_writing_configuration(self):
        for name in REQUIRED:
            with self.subTest(name=name):
                env = {key: value for key, value in self.env.items() if key != name}
                result = self.initialize(env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Set {name} in Codespaces secrets", result.stderr)
                self.assertFalse((Path(self.home.name) / ".aws").exists())


if __name__ == "__main__":
    unittest.main()
