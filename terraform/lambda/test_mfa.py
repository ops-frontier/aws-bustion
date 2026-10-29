import base64
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

from botocore.exceptions import ClientError
from botocore.stub import Stubber

import boto3
import github_auth as auth
import mfa
import test_github_auth as auth_tests


SECRET = b"12345678901234567890"
NOW = 1234567890


def conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


class OTPDatabase:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def get_item(self, **request):
        with self.lock:
            item = self.items.get(request["Key"]["login"]["S"])
            return {"Item": deepcopy(item)} if item else {}

    def put_item(self, **request):
        with self.lock:
            item = request["Item"]
            old = self.items.get(item["login"]["S"])
            if old:
                values = request["ExpressionAttributeValues"]
                if (
                    old["active"]["BOOL"]
                    or int(old["pending_until"]["N"]) > int(values[":now"]["N"])
                    or old["user_id"] != values[":user"]
                ):
                    raise conditional_failure()
            self.items[item["login"]["S"]] = deepcopy(item)

    def update_item(self, **request):
        with self.lock:
            item = self.items[request["Key"]["login"]["S"]]
            values = request["ExpressionAttributeValues"]
            if item["generation"] != values[":generation"]:
                raise conditional_failure()
            expression = request["UpdateExpression"]
            if expression == "SET attempts = :zero, window_until = :until":
                if int(item["window_until"]["N"]) > int(values[":now"]["N"]):
                    raise conditional_failure()
                item.update(attempts=values[":zero"], window_until=values[":until"])
            elif expression == "ADD attempts :one":
                if int(item["attempts"]["N"]) >= int(values[":limit"]["N"]):
                    raise conditional_failure()
                item["attempts"] = {"N": str(int(item["attempts"]["N"]) + 1)}
            else:
                if (
                    item["active"] != values[":active"]
                    or int(item["last_step"]["N"]) >= int(values[":step"]["N"])
                    or (":now" in values and int(item["pending_until"]["N"]) <= int(values[":now"]["N"]))
                ):
                    raise conditional_failure()
                item.update(last_step=values[":step"], attempts=values[":zero"])
                if ":yes" in values:
                    item["active"] = values[":yes"]
        return {}


class MFATests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(mfa.time, "time", return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.db = OTPDatabase()
        self.kms = MagicMock()
        self.kms.encrypt.return_value = {"CiphertextBlob": b"opaque-ciphertext"}
        self.kms.decrypt.return_value = {"Plaintext": SECRET}
        self.store = mfa.MFAStore(self.db, self.kms, "otp-table", "key-id")
        self.record = self.store.prepare("test-user", "123")

    def activate(self):
        self.store.verify("test-user", mfa.totp(SECRET, NOW // 30), enrolling=True, user_id="123")

    def assert_rejected(self, reason, *args, **kwargs):
        with self.assertRaisesRegex(mfa.MFAError, reason):
            self.store.verify(*args, **kwargs)

    def test_rfc_6238_vectors_six_digits(self):
        for timestamp, expected in (
            (59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
            (1234567890, "005924"), (2000000000, "279037"),
        ):
            self.assertEqual(mfa.totp(SECRET, timestamp // 30), expected)

    def test_only_ciphertext_is_persisted_with_bound_kms_context(self):
        self.assertNotIn(SECRET.decode(), repr(self.db.items))
        self.assertNotIn(base64.b32encode(SECRET).decode(), repr(self.db.items))
        self.assertEqual(
            self.kms.encrypt.call_args.kwargs["EncryptionContext"],
            {"purpose": "bustion-totp", "login": "test-user"},
        )
        self.assertEqual(self.store.secret(self.record), SECRET)
        self.kms.decrypt.assert_called_once_with(
            KeyId="key-id", CiphertextBlob=b"opaque-ciphertext",
            EncryptionContext={"purpose": "bustion-totp", "login": "test-user"},
        )

    def test_pending_seed_cannot_authenticate_until_confirmed(self):
        self.assert_rejected("not_enrolled", "test-user", mfa.totp(SECRET, NOW // 30))
        self.assertFalse(self.record["active"]["BOOL"])
        self.activate()
        self.assertTrue(self.store.get("test-user")["active"]["BOOL"])
        self.assert_rejected("replayed_or_changed", "test-user", mfa.totp(SECRET, NOW // 30))
        self.clock.return_value += 30
        self.store.verify("test-user", mfa.totp(SECRET, (NOW + 30) // 30))

    def test_active_seed_is_never_rotated_or_disclosed_by_prepare(self):
        self.activate()
        self.clock.return_value += 1000
        active = self.store.prepare("test-user", "123")
        self.assertTrue(active["active"]["BOOL"])
        self.assertEqual(active["generation"], self.record["generation"])
        self.kms.encrypt.assert_called_once()
        with self.assertRaisesRegex(mfa.MFAError, "identity_conflict"):
            self.store.prepare("test-user", "another-user")

    def test_pending_seed_is_stable_until_expiry(self):
        self.assertEqual(self.store.prepare("test-user", "123"), self.record)
        self.clock.return_value += 600
        self.assert_rejected(
            "enrollment_expired", "test-user", "005924", enrolling=True, user_id="123",
        )
        replacement = self.store.prepare("test-user", "123")
        self.assertNotEqual(replacement["generation"], self.record["generation"])
        self.assertEqual(self.kms.encrypt.call_count, 2)

    def test_five_attempts_then_lockout_and_window_reset(self):
        self.activate()
        for _ in range(mfa.MAX_ATTEMPTS):
            self.assert_rejected("invalid_code", "test-user", "invalid")
        self.clock.return_value += 30
        valid = mfa.totp(SECRET, (NOW + 30) // 30)
        self.assert_rejected("rate_limited", "test-user", valid)
        self.clock.return_value = NOW + 300
        self.store.verify("test-user", mfa.totp(SECRET, (NOW + 300) // 30))
        self.assertEqual(self.store.get("test-user")["attempts"]["N"], "0")

    def test_wrong_code_does_not_activate_registration(self):
        self.assert_rejected(
            "invalid_code", "test-user", "12345x", enrolling=True, user_id="123",
        )
        self.assertFalse(self.store.get("test-user")["active"]["BOOL"])

    def test_identity_conflict_unknown_user_and_invalid_login_are_rejected(self):
        self.assert_rejected("not_enrolled", "unknown", "005924")
        self.assert_rejected("invalid_login", "domain\\test-user", "005924")
        self.assert_rejected(
            "identity_conflict", "test-user", "005924", enrolling=True, user_id="456",
        )

    def test_clock_skew_and_old_code(self):
        self.activate()
        self.clock.return_value += 60
        self.store.verify("test-user", mfa.totp(SECRET, (NOW + 30) // 30))
        self.assert_rejected("invalid_code", "test-user", mfa.totp(SECRET, (NOW - 30) // 30))

    def test_concurrent_reuse_accepts_only_one_request(self):
        self.activate()
        self.clock.return_value += 30
        code = mfa.totp(SECRET, (NOW + 30) // 30)
        barrier = threading.Barrier(2)

        def verify():
            barrier.wait()
            try:
                self.store.verify("test-user", code)
            except mfa.MFAError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(lambda _: verify(), range(2))), 1)

    def test_storage_and_kms_failures_are_not_success(self):
        self.kms.decrypt.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Decrypt")
        with self.assertRaises(ClientError):
            self.activate()
        self.assertFalse(self.store.get("test-user")["active"]["BOOL"])

    def test_dynamodb_request_shapes_match_sdk(self):
        db = boto3.client(
            "dynamodb", region_name="ap-northeast-1",
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )
        with Stubber(db) as stub:
            stub.add_response("get_item", {})
            stub.add_response("put_item", {})
            mfa.MFAStore(db, self.kms, "otp-table", "key-id").prepare("new-user", "123")


class EnrollmentTests(auth_tests.AuthTestCase):
    login = auth_tests.WindowsTests.login
    start = auth_tests.WindowsTests.start
    registered = auth_tests.WindowsTests.registered
    owned_user = auth_tests.WindowsTests.owned_user
    missing_directory_user = auth_tests.WindowsTests.missing_directory_user

    def setUp(self):
        super().setUp()
        self.mfa.prepare.return_value = {
            "active": {"BOOL": False}, "login": {"S": "changeable-name"},
        }
        self.mfa.secret.return_value = SECRET

    def test_qr_is_local_svg_and_contains_standard_totp_uri(self):
        callback = self.login()
        with patch.object(auth.qrcode, "make", wraps=auth.qrcode.make) as qr:
            response = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
        self.assertEqual(response["statusCode"], 200)
        body = response["body"]
        self.assertIn("<svg", body)
        self.assertIn('name="otp"', body)
        svg = body[body.index("<svg"):body.index("</svg>") + 6]
        ElementTree.fromstring(svg)
        uri = urlparse(qr.call_args.args[0])
        self.assertEqual(uri.scheme, "otpauth")
        self.assertEqual(uri.netloc, "totp")
        params = parse_qs(uri.query)
        self.assertEqual(params["secret"], [base64.b32encode(SECRET).decode()])
        self.assertEqual(params["digits"], ["6"])
        self.assertEqual(params["period"], ["30"])
        self.assertEqual(params["algorithm"], ["SHA1"])
        self.assertNotIn("data:", body)
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", response["headers"]["Content-Security-Policy"])

    def test_password_alone_does_not_create_ad_user_or_workspace(self):
        callback = self.login()
        response = self.start(callback)
        self.assertEqual(response["statusCode"], 200)
        self.assertIn('name="otp"', response["body"])
        self.ds_data.create_user.assert_not_called()
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()

    def test_invalid_otp_does_not_create_any_aws_user_resources(self):
        callback = self.login()
        self.mfa.verify.side_effect = mfa.MFAError("invalid_code")
        response = auth.lambda_handler(
            self.session_event(callback, "POST /windows", {
                "password": "ValidPassword123!", "confirm": "ValidPassword123!", "otp": "000000",
            }), None,
        )
        self.assertEqual(response["statusCode"], 403)
        self.ds_data.create_user.assert_not_called()
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()

    def test_otp_is_checked_before_directory_registration(self):
        callback = self.login()
        self.missing_directory_user()
        order = MagicMock()
        order.attach_mock(self.mfa.verify, "verify")
        order.attach_mock(self.ds_data.create_user, "create_user")
        order.attach_mock(self.ds.reset_user_password, "set_password")
        response = auth.lambda_handler(
            self.session_event(callback, "POST /windows", {
                "password": "ValidPassword123!", "confirm": "ValidPassword123!", "otp": "005924",
            }), None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual([call[0] for call in order.mock_calls], ["verify", "create_user", "set_password"])
        self.mfa.verify.assert_called_once_with(
            "changeable-name", "005924", enrolling=True, user_id="123456",
        )

    def test_existing_user_enrolls_without_resetting_password(self):
        callback = self.login()
        self.registered(callback)
        response = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
        self.assertIn('name="otp"', response["body"])
        self.assertNotIn('name="password"', response["body"])
        self.workspaces.create_workspaces.assert_not_called()
        response = auth.lambda_handler(
            self.session_event(callback, "POST /windows", {"otp": "005924"}), None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.ds.reset_user_password.assert_not_called()
        self.mfa.verify.assert_called_once()

    def test_active_seed_is_not_shown_again(self):
        callback = self.login()
        self.mfa.prepare.return_value = {"active": {"BOOL": True}}
        response = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
        self.assertNotIn("<svg", response["body"])
        self.mfa.secret.assert_not_called()

    def test_registration_with_real_store_is_persistent_and_code_is_consumed(self):
        callback = self.login()
        self.missing_directory_user()
        kms = MagicMock()
        kms.encrypt.return_value = {"CiphertextBlob": b"opaque-ciphertext"}
        kms.decrypt.return_value = {"Plaintext": SECRET}
        store = mfa.MFAStore(OTPDatabase(), kms, "otp-table", "key-id")
        with (
            patch.object(auth, "mfa_store", return_value=store),
            patch.object(mfa.time, "time", return_value=NOW) as clock,
        ):
            initial = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
            self.assertIn("<svg", initial["body"])
            self.assertFalse(store.get("changeable-name")["active"]["BOOL"])
            response = auth.lambda_handler(
                self.session_event(callback, "POST /windows", {
                    "password": "ValidPassword123!", "confirm": "ValidPassword123!",
                    "otp": mfa.totp(SECRET, NOW // 30),
                }), None,
            )
            self.assertEqual(response["statusCode"], 200)
            self.assertTrue(store.get("changeable-name")["active"]["BOOL"])
            with self.assertRaisesRegex(mfa.MFAError, "replayed_or_changed"):
                store.verify("changeable-name", mfa.totp(SECRET, NOW // 30))
            clock.return_value += 30
            store.verify("changeable-name", mfa.totp(SECRET, (NOW + 30) // 30))
            self.ds_data.describe_user.side_effect = None
            self.ds_data.describe_user.return_value = self.owned_user()
            revisit = auth.lambda_handler(self.session_event(callback, "GET /windows"), None)
            self.assertNotIn("<svg", revisit["body"])
        self.ds.reset_user_password.assert_called_once()


if __name__ == "__main__":
    unittest.main()
