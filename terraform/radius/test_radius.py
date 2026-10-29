import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import BotoCoreError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
from mfa import MFAError


RADIUS = types.SimpleNamespace(
    RLM_MODULE_OK=2, RLM_MODULE_REJECT=0, RLM_MODULE_FAIL=1,
    L_ERR=3, L_INFO=6, radlog=MagicMock(),
)
with patch.dict(sys.modules, {"radiusd": RADIUS}):
    spec = importlib.util.spec_from_file_location("radius_auth", Path(__file__).with_name("radius_auth.py"))
    radius_auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(radius_auth)

spec = importlib.util.spec_from_file_location("configure", Path(__file__).with_name("configure.py"))
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class RadiusTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        store_patch = patch.object(radius_auth, "STORE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        RADIUS.radlog.reset_mock()

    def test_radius_pap_password_is_only_the_otp(self):
        result = radius_auth.authenticate((("User-Name", "Test-User"), ("User-Password", "123456")))
        self.assertEqual(result, RADIUS.RLM_MODULE_OK)
        self.store.verify.assert_called_once_with("test-user", "123456")
        self.assertNotIn("123456", repr(RADIUS.radlog.mock_calls))

    def test_invalid_replay_and_lockout_fail_closed(self):
        for reason in ("invalid_code", "replayed_or_changed", "rate_limited", "not_enrolled"):
            self.store.verify.side_effect = MFAError(reason)
            self.assertEqual(
                radius_auth.authenticate((("User-Name", "test-user"), ("User-Password", "123456"))),
                RADIUS.RLM_MODULE_REJECT,
            )

    def test_dependency_failure_does_not_accept(self):
        self.store.verify.side_effect = BotoCoreError()
        self.assertEqual(
            radius_auth.authenticate((("User-Name", "test-user"), ("User-Password", "123456"))),
            RADIUS.RLM_MODULE_FAIL,
        )
        self.assertIn("BotoCoreError", repr(RADIUS.radlog.mock_calls))

    def test_malformed_and_duplicate_attributes_are_rejected(self):
        for attributes in (
            (), (("User-Name", "test-user"),),
            (("User-Name", "test-user"), ("User-Password", "password123456")),
            (("User-Name", "test-user"), ("User-Password", "123456"), ("User-Password", "654321")),
        ):
            self.assertEqual(radius_auth.authenticate(attributes), RADIUS.RLM_MODULE_REJECT)
        self.store.verify.assert_not_called()

    def test_uninitialized_module_fails_closed(self):
        with patch.object(radius_auth, "STORE", None):
            self.assertEqual(
                radius_auth.authenticate((("User-Name", "test-user"), ("User-Password", "123456"))),
                RADIUS.RLM_MODULE_FAIL,
            )

    def test_shared_secret_is_fetched_at_boot_not_in_user_data(self):
        config = {
            "region": "ap-northeast-1", "secret": "secret-arn",
            "clients": ["10.20.0.10", "10.20.16.10"],
        }
        source = MagicMock()
        source.read_text.return_value = json.dumps(config)
        destination = MagicMock()
        secret = "aB12" * 16
        with (
            patch.object(configure, "Path", side_effect=[source, destination]),
            patch.object(configure.boto3, "client") as client,
            patch.object(configure.os, "chmod") as chmod,
            patch.object(configure.os, "chown"),
            patch.object(configure.grp, "getgrnam"),
        ):
            client.return_value.get_secret_value.return_value = {"SecretString": secret}
            configure.main()
        text = destination.write_text.call_args.args[0]
        self.assertIn("ipaddr = 10.20.0.10", text)
        self.assertIn("ipaddr = 10.20.16.10", text)
        self.assertEqual(text.count("require_message_authenticator = yes"), 2)
        self.assertNotIn("0.0.0.0", text)
        self.assertEqual(chmod.call_args.args[1], 0o640)


if __name__ == "__main__":
    unittest.main()
