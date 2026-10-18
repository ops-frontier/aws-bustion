import base64
import hashlib
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse

from botocore.exceptions import ClientError
from botocore.stub import Stubber

import github_auth as auth


class StateStore:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        if kwargs["ConditionExpression"] != "attribute_not_exists(state_id)":
            raise AssertionError("State creation must not overwrite an existing entry.")
        item = kwargs["Item"]
        self.items[item["state_id"]["S"]] = item

    def delete_item(self, **kwargs):
        if kwargs["ConditionExpression"] != "expires_at > :now":
            raise AssertionError("State consumption must atomically check expiration.")
        if kwargs["ReturnValues"] != "ALL_OLD":
            raise AssertionError("State consumption must return the consumed verifier.")
        key = kwargs["Key"]["state_id"]["S"]
        item = self.items.get(key)
        now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
        if item is None or int(item["expires_at"]["N"]) <= now:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem"
            )
        return {"Attributes": self.items.pop(key)}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "BASE_URL": "https://example.execute-api.ap-northeast-1.amazonaws.com",
                "GITHUB_ORGANIZATION": "example-org",
                "OAUTH_SECRET_ARN": "test-secret",
                "STATE_TABLE": "test-state",
                "APPSTREAM_STACK": "test-stack",
                "APPSTREAM_FLEET": "test-fleet",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.state = StateStore()
        self.secret = MagicMock()
        self.secret.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {"client_id": "test-client", "client_secret": "never-log-secret"}
            )
        }
        self.appstream = MagicMock()
        self.appstream.describe_fleets.return_value = {
            "Fleets": [{
                "State": "RUNNING",
                "ComputeCapacityStatus": {
                    "Desired": 1,
                    "Available": 1,
                    "InUse": 0,
                    "Running": 1,
                },
            }]
        }
        self.appstream.create_streaming_url.return_value = {
            "StreamingURL": "https://stream.example.com/session"
        }
        self.clients = {
            "dynamodb": self.state,
            "secretsmanager": self.secret,
            "appstream": self.appstream,
        }
        client_patch = patch.object(auth, "aws_client", side_effect=self.clients.__getitem__)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        github_patch = patch.object(auth, "github_json")
        self.github = github_patch.start()
        self.addCleanup(github_patch.stop)
        self.github.side_effect = [
            {"access_token": "never-log-token"},
            {"id": 123456, "login": "changeable-name"},
            {"state": "active"},
        ]

    def begin(self):
        result = auth.lambda_handler({"routeKey": "GET /login"}, None)
        self.assertEqual(result["statusCode"], 302)
        params = parse_qs(urlparse(result["headers"]["Location"]).query)
        event = {
            "routeKey": "GET /callback",
            "rawQueryString": urlencode({"state": params["state"][0], "code": "code"}),
            "cookies": [result["cookies"][0].split(";")[0]],
        }
        return result, params, event

    def test_login_cookie_state_and_pkce(self):
        result, params, _ = self.begin()
        state = params["state"][0]
        self.assertEqual(len(state), 43)
        self.assertEqual(params["scope"], ["read:org"])
        self.assertEqual(params["redirect_uri"], [os.environ["BASE_URL"] + "/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        item = self.state.items[hashlib.sha256(state.encode()).hexdigest()]
        verifier = item["verifier"]["S"]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.assertEqual(params["code_challenge"], [expected.rstrip(b"=").decode()])
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=600"):
            self.assertIn(flag, result["cookies"][0])
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.appstream.create_streaming_url.assert_not_called()

    def test_active_member_launches_with_stable_id_and_60_second_url(self):
        _, _, event = self.begin()
        with self.assertLogs(auth.LOGGER, level="INFO") as logs:
            result = auth.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 302)
        self.assertEqual(result["headers"]["Location"], "https://stream.example.com/session")
        self.assertEqual(result["headers"]["Referrer-Policy"], "no-referrer")
        self.assertIn("Max-Age=0", result["cookies"][0])
        self.assertEqual(self.state.items, {})
        self.appstream.create_streaming_url.assert_called_once_with(
            StackName="test-stack", FleetName="test-fleet", UserId="gh-123456", Validity=60
        )
        self.appstream.update_fleet.assert_not_called()
        self.assertEqual(
            self.github.call_args_list[2].args[0],
            "https://api.github.com/user/memberships/orgs/example-org",
        )
        self.assertTrue(self.github.call_args_list[2].kwargs["membership"])
        self.assertIn("code_verifier", self.github.call_args_list[0].kwargs["form"])
        self.assertNotIn("never-log", " ".join(logs.output) + json.dumps(result))

    def test_missing_mismatched_or_duplicate_state_is_rejected(self):
        _, params, event = self.begin()
        events = [
            {**event, "cookies": []},
            {**event, "cookies": [f"{auth.COOKIE_NAME}={'x' * 43}"]},
            {**event, "rawQueryString": "code=code"},
            {**event, "rawQueryString": event["rawQueryString"] + "&state=" + params["state"][0]},
        ]
        for invalid_event in events:
            with self.subTest(event=invalid_event):
                self.assertEqual(auth.lambda_handler(invalid_event, None)["statusCode"], 400)
        self.github.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()
        self.assertEqual(len(self.state.items), 1)

    def test_empty_fleet_is_started_before_redirecting_to_appstream(self):
        fleet = self.appstream.describe_fleets.return_value["Fleets"][0]
        fleet["ComputeCapacityStatus"].update(
            Desired=0, Available=0, Running=0
        )
        _, _, event = self.begin()
        result = auth.lambda_handler(event, None)
        self.appstream.describe_fleets.assert_called_once_with(Names=["test-fleet"])
        self.appstream.update_fleet.assert_called_once_with(
            Name="test-fleet", ComputeCapacity={"DesiredInstances": 1}
        )
        self.appstream.create_streaming_url.assert_not_called()
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(result["headers"]["Retry-After"], "60")
        self.assertIn('href="/login"', result["body"])
        self.assertIn('http-equiv="refresh"', result["body"])
        self.assertNotIn("Location", result["headers"])
        self.assertIn("Max-Age=0", result["cookies"][0])

    def test_pending_fleet_waits_without_requesting_more_instances(self):
        fleet = self.appstream.describe_fleets.return_value["Fleets"][0]
        fleet["ComputeCapacityStatus"].update(
            Desired=1, Available=0, Running=0
        )
        _, _, event = self.begin()
        result = auth.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.appstream.update_fleet.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()

    def test_fresh_logins_wait_then_launch_without_repeated_scale_out(self):
        capacity = self.appstream.describe_fleets.return_value["Fleets"][0][
            "ComputeCapacityStatus"
        ]
        capacity.update(Desired=0, Available=0, Running=0)

        def start_instance(**kwargs):
            capacity["Desired"] = kwargs["ComputeCapacity"]["DesiredInstances"]

        self.appstream.update_fleet.side_effect = start_instance
        for available, expected_status in ((0, 503), (0, 503), (1, 302)):
            with self.subTest(available=available, expected_status=expected_status):
                capacity["Available"] = available
                self.github.side_effect = [
                    {"access_token": "token"}, {"id": 123}, {"state": "active"}
                ]
                _, _, event = self.begin()
                result = auth.lambda_handler(event, None)
                self.assertEqual(result["statusCode"], expected_status)
                if expected_status == 503:
                    self.assertEqual(
                        result["headers"]["Content-Type"], "text/html; charset=utf-8"
                    )
                    self.assertEqual(result["headers"]["Cache-Control"], "no-store")
                    self.appstream.create_streaming_url.assert_not_called()
                self.assertEqual(self.state.items, {})
        self.appstream.update_fleet.assert_called_once()
        self.appstream.create_streaming_url.assert_called_once()
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.appstream.update_fleet.assert_called_once()

    def test_no_spare_capacity_does_not_block_existing_session_reconnection(self):
        capacity = self.appstream.describe_fleets.return_value["Fleets"][0][
            "ComputeCapacityStatus"
        ]
        capacity.update(Available=0, InUse=1)
        _, _, event = self.begin()
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 302)
        self.appstream.update_fleet.assert_not_called()
        self.appstream.create_streaming_url.assert_called_once()

    def test_missing_or_stopped_fleet_is_not_started(self):
        for fleets in ([], [{"State": "STOPPED"}], [{"State": "STARTING"}]):
            with self.subTest(fleets=fleets):
                self.appstream.describe_fleets.return_value = {"Fleets": fleets}
                self.github.side_effect = [
                    {"access_token": "token"}, {"id": 123}, {"state": "active"}
                ]
                _, _, event = self.begin()
                result = auth.lambda_handler(event, None)
                self.assertEqual(result["statusCode"], 503)
                self.assertIn("administrator", result["body"])
                self.assertNotIn("Retry-After", result["headers"])
        self.appstream.update_fleet.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()

    def test_capacity_api_failures_do_not_claim_fleet_is_starting(self):
        capacity = self.appstream.describe_fleets.return_value["Fleets"][0][
            "ComputeCapacityStatus"
        ]
        capacity.update(Desired=0, Available=0, Running=0)
        for operation in ("describe_fleets", "update_fleet"):
            with self.subTest(operation=operation):
                method = getattr(self.appstream, operation)
                method.side_effect = ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "sensitive"}},
                    operation,
                )
                self.github.side_effect = [
                    {"access_token": "token"}, {"id": 123}, {"state": "active"}
                ]
                _, _, event = self.begin()
                with self.assertLogs(auth.LOGGER, level="ERROR") as logs:
                    result = auth.lambda_handler(event, None)
                self.assertEqual(result["statusCode"], 503)
                self.assertNotIn("Retry-After", result["headers"])
                self.assertNotIn("sensitive", " ".join(logs.output) + json.dumps(result))
                method.side_effect = None
        self.appstream.create_streaming_url.assert_not_called()

    def test_expired_state_is_rejected_even_before_ttl_deletion(self):
        _, _, event = self.begin()
        for item in self.state.items.values():
            item["expires_at"] = {"N": "0"}
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.github.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()

    def test_state_cannot_be_replayed(self):
        _, _, event = self.begin()
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 302)
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.appstream.create_streaming_url.assert_called_once()

    def test_state_is_consumed_before_external_requests(self):
        _, _, event = self.begin()
        auth.consume_state(event)
        with self.assertRaises(auth.AuthError) as raised:
            auth.consume_state(event)
        self.assertEqual(raised.exception.code, "expired_state")
        self.github.assert_not_called()

    def test_denied_authorization_consumes_state_without_exchanging_code(self):
        _, params, event = self.begin()
        event["rawQueryString"] = urlencode(
            {"state": params["state"][0], "error": "access_denied"}
        )
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.assertEqual(self.state.items, {})
        self.github.assert_not_called()

    def test_missing_or_duplicate_code_is_rejected(self):
        for code_query in ("", "&code=", "&code=a&code=b"):
            with self.subTest(code_query=code_query):
                _, params, event = self.begin()
                event["rawQueryString"] = "state=" + params["state"][0] + code_query
                self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.github.assert_not_called()

    def test_pending_or_missing_membership_is_denied(self):
        for membership in ({"state": "pending"}, {}, {"state": "inactive"}):
            with self.subTest(membership=membership):
                _, _, event = self.begin()
                self.github.side_effect = [
                    {"access_token": "token"}, {"id": 123}, membership
                ]
                self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 403)
        self.appstream.create_streaming_url.assert_not_called()
        self.appstream.describe_fleets.assert_not_called()
        self.appstream.update_fleet.assert_not_called()

    def test_github_failures_never_launch_appstream(self):
        for status in (403, 502):
            with self.subTest(status=status):
                _, _, event = self.begin()
                self.github.side_effect = auth.AuthError(status, "test_failure", "Failed.")
                self.assertEqual(auth.lambda_handler(event, None)["statusCode"], status)
        self.appstream.create_streaming_url.assert_not_called()
        self.appstream.describe_fleets.assert_not_called()
        self.appstream.update_fleet.assert_not_called()

    def test_failed_token_exchange_is_rejected(self):
        _, _, event = self.begin()
        self.github.side_effect = [{"error": "bad_verification_code"}]
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.appstream.create_streaming_url.assert_not_called()

    def test_invalid_user_ids_are_rejected(self):
        for user_id in (None, True, -1, 0, "123", 10**30):
            with self.subTest(user_id=user_id):
                _, _, event = self.begin()
                self.github.side_effect = [{"access_token": "token"}, {"id": user_id}]
                self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 502)
        self.appstream.create_streaming_url.assert_not_called()

    def test_unconfigured_secret_is_reported(self):
        for value in ("", "not-json", "[]", "{}", '{"client_id":true,"client_secret":"x"}'):
            with self.subTest(value=value):
                self.secret.get_secret_value.return_value = {"SecretString": value}
                self.assertEqual(
                    auth.lambda_handler({"routeKey": "GET /login"}, None)["statusCode"], 503
                )
        self.assertEqual(self.state.items, {})

    def test_missing_secret_version_is_reported_without_sensitive_details(self):
        self.secret.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "sensitive"}},
            "GetSecretValue",
        )
        with self.assertLogs(auth.LOGGER, level="ERROR") as logs:
            result = auth.lambda_handler({"routeKey": "GET /login"}, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertNotIn("sensitive", " ".join(logs.output) + json.dumps(result))

    def test_unavailable_fleet_returns_error_not_redirect(self):
        _, _, event = self.begin()
        self.appstream.create_streaming_url.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotAvailableException"}}, "CreateStreamingURL"
        )
        result = auth.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertNotIn("Location", result["headers"])
        self.assertIn("Max-Age=0", result["cookies"][0])

    def test_unknown_route_is_not_found(self):
        self.assertEqual(
            auth.lambda_handler({"routeKey": "POST /callback"}, None)["statusCode"], 404
        )
        self.secret.get_secret_value.assert_not_called()


class FleetApiTests(unittest.TestCase):
    def test_cold_start_matches_aws_sdk_request_and_response_models(self):
        client = auth.boto3.client(
            "appstream", region_name="ap-northeast-1",
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )
        fleet = {
            "Name": "test-fleet",
            "Arn": "arn:aws:appstream:ap-northeast-1:123456789012:fleet/test-fleet",
            "InstanceType": "stream.standard.xlarge",
            "FleetType": "ALWAYS_ON",
            "State": "RUNNING",
            "ComputeCapacityStatus": {
                "Desired": 0, "Running": 0, "InUse": 0, "Available": 0,
            },
        }
        with Stubber(client) as stub, patch.dict(
            os.environ, {"APPSTREAM_FLEET": "test-fleet"}
        ):
            stub.add_response(
                "describe_fleets", {"Fleets": [fleet]}, {"Names": ["test-fleet"]}
            )
            stub.add_response(
                "update_fleet", {},
                {"Name": "test-fleet", "ComputeCapacity": {"DesiredInstances": 1}},
            )
            self.assertFalse(auth.ensure_fleet_capacity(client))
            stub.assert_no_pending_responses()


class GitHubRequestTests(unittest.TestCase):
    def test_http_failures_and_membership_404(self):
        for status, membership, expected in (
            (404, True, 403), (403, True, 502), (429, False, 502), (500, False, 502)
        ):
            with self.subTest(status=status, membership=membership):
                with patch.object(auth, "build_opener") as opener:
                    opener.return_value.open.side_effect = HTTPError(
                        "https://api.github.com/test", status, "error", {}, io.BytesIO()
                    )
                    with self.assertRaises(auth.AuthError) as raised:
                        auth.github_json("https://api.github.com/test", membership=membership)
                    self.assertEqual(raised.exception.status, expected)

    def test_timeout_network_and_invalid_json_fail_closed(self):
        for failure in (TimeoutError(), URLError("offline"), ValueError("bad-json")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(auth, "build_opener") as opener:
                    opener.return_value.open.side_effect = failure
                    with self.assertRaises(auth.AuthError) as raised:
                        auth.github_json("https://api.github.com/test")
                    self.assertEqual(raised.exception.status, 502)

    def test_non_object_json_is_rejected(self):
        with patch.object(auth, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = io.StringIO("[]")
            with self.assertRaises(auth.AuthError):
                auth.github_json("https://api.github.com/test")

    def test_token_post_and_api_bearer_headers(self):
        with patch.object(auth, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.side_effect = [
                io.StringIO('{"access_token":"token"}'), io.StringIO('{"id":123}')
            ]
            auth.github_json(
                "https://github.com/login/oauth/access_token",
                form={"code": "a+b", "code_verifier": "verifier"},
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(parse_qs(request.data.decode())["code"], ["a+b"])
            auth.github_json("https://api.github.com/user", token="token")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer token")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 3)
            self.assertIsInstance(opener.call_args.args[0], auth.NoRedirects)

    def test_upstream_redirects_are_not_followed(self):
        self.assertIsNone(
            auth.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other")
        )


if __name__ == "__main__":
    unittest.main()
