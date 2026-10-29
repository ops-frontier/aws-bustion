import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("wait_radius_ready.sh")


class ReadinessTests(unittest.TestCase):
    def run_waiter(self, status="Success", api_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aws = root / "aws"
            aws.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'echo "$*" >> "$CALLS"\n'
                'if [[ "$API_FAILURE" == 1 ]]; then exit 7; fi\n'
                'case "$1 $2" in\n'
                '  "ssm describe-instance-information") echo Online ;;\n'
                '  "ssm send-command") echo command-123 ;;\n'
                '  "ssm list-command-invocations")\n'
                '    if [[ ! -f "$SEEN_PENDING" ]]; then\n'
                '      touch "$SEEN_PENDING"; echo Pending\n'
                '    else echo "$RESULT"; fi ;;\n'
                '  "ssm get-command-invocation") echo bootstrap-failed ;;\n'
                "  *) exit 8 ;;\n"
                "esac\n"
            )
            aws.chmod(0o700)
            sleep = root / "sleep"
            sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
            sleep.chmod(0o700)
            calls = root / "calls"
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "RADIUS_INSTANCE_ID": "i-test",
                    "AWS_REGION": "ap-northeast-1",
                    "CALLS": str(calls),
                    "SEEN_PENDING": str(root / "seen"),
                    "RESULT": status,
                    "API_FAILURE": str(int(api_failure)),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, calls.read_text()

    def test_waits_for_actual_command_success(self):
        result, calls = self.run_waiter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("ssm list-command-invocations"), 2)
        self.assertIn("cloud-init status --wait", calls)
        self.assertIn("systemctl is-active --quiet freeradius", calls)
        self.assertIn("freeradius -C", calls)
        self.assertIn("checks succeeded", result.stdout)

    def test_failed_or_timed_out_execution_blocks_activation(self):
        for status in ("Failed", "TimedOut", "Cancelled"):
            with self.subTest(status=status):
                result, calls = self.run_waiter(status)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"readiness failed: {status}", result.stderr)
                self.assertIn("ssm get-command-invocation", calls)
                self.assertIn("bootstrap-failed", result.stdout)
                self.assertNotIn("checks succeeded", result.stdout)

    def test_aws_error_is_not_treated_as_ready(self):
        result, calls = self.run_waiter(api_failure=True)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("ssm send-command", calls)
        self.assertNotIn("checks succeeded", result.stdout)


if __name__ == "__main__":
    unittest.main()
