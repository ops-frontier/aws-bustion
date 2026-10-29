import base64
import hashlib
import io
import json
import os
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse

from botocore.exceptions import ClientError
from botocore.stub import Stubber

import github_auth as auth


class StateStore:
    def __init__(self):
        self.items = {}
        self.sessions = {}
        self.users = {}

    def table(self, name, key):
        if name == "test-users":
            return self.users
        return self.sessions if key.startswith("session:") else self.items

    def put_item(self, **kwargs):
        if kwargs["ConditionExpression"] != "attribute_not_exists(state_id)":
            raise AssertionError("State creation must not overwrite an existing entry.")
        item = kwargs["Item"]
        key = item["state_id"]["S"]
        self.table(kwargs["TableName"], key)[key] = item

    def get_item(self, **kwargs):
        key = next(iter(kwargs["Key"].values()))["S"]
        item = self.table(kwargs["TableName"], key).get(key)
        return {"Item": deepcopy(item)} if item else {}

    def update_item(self, **kwargs):
        key = next(iter(kwargs["Key"].values()))["S"]
        item = self.table(kwargs["TableName"], key)[key]
        values = kwargs["ExpressionAttributeValues"]
        condition = kwargs.get("ConditionExpression", "")
        if (
            condition.startswith("attribute_not_exists(lease_until)")
            and int(item.get("lease_until", {"N": "0"})["N"]) >= int(values[":now"]["N"])
        ) or (
            condition == "expires_at > :now"
            and int(item.get("expires_at", {"N": "0"})["N"]) <= int(values[":now"]["N"])
        ) or (condition == "lease = :lease" and item.get("lease") != values[":lease"]):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        expressions = kwargs["UpdateExpression"]
        if expressions == "REMOVE lease, lease_until":
            item.pop("lease", None)
            item.pop("lease_until", None)
        elif expressions == "REMOVE workspace_requested SET ad_ready = :ready":
            item.pop("workspace_requested", None)
            item["ad_ready"] = values[":ready"]
        else:
            for assignment in expressions.removeprefix("SET ").split(", "):
                field, value = assignment.split(" = ")
                item[field] = deepcopy(values[value])
        return {}

    def transact_write_items(self, **kwargs):
        puts = [operation["Put"] for operation in kwargs["TransactItems"]]
        if any(p["Item"]["principal"]["S"] in self.users for p in puts):
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException"},
                 "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]},
                "TransactWriteItems",
            )
        for put in puts:
            self.users[put["Item"]["principal"]["S"]] = deepcopy(put["Item"])
        return {}

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


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "BASE_URL": "https://example.execute-api.ap-northeast-1.amazonaws.com",
                "AWS_REGION": "ap-northeast-1",
                "GITHUB_ORGANIZATION": "example-org",
                "OAUTH_SECRET_ARN": "test-secret",
                "STATE_TABLE": "test-state",
                "APPSTREAM_STACK": "test-stack",
                "APPSTREAM_FLEET": "test-fleet",
                "USERS_TABLE": "test-users",
                "DIRECTORY_ID": "d-1234567890",
                "WORKSPACE_BUNDLE_ID": "wsb-96cqc2ksn",
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
        self.ds_data = MagicMock()
        self.ds = MagicMock()
        self.workspaces = MagicMock()
        self.clients.update({"ds-data": self.ds_data, "ds": self.ds, "workspaces": self.workspaces})
        self.workspaces.describe_workspaces.return_value = {"Workspaces": []}
        self.workspaces.create_workspaces.return_value = {
            "PendingRequests": [{"WorkspaceId": "ws-123456789", "State": "PENDING"}],
            "FailedRequests": [],
        }
        client_patch = patch.object(auth, "aws_client", side_effect=self.clients.__getitem__)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        github_patch = patch.object(auth, "github_json")
        self.github = github_patch.start()
        self.addCleanup(github_patch.stop)
        mfa_patch = patch.object(auth, "mfa_store")
        self.mfa = mfa_patch.start().return_value
        self.addCleanup(mfa_patch.stop)
        self.mfa.prepare.return_value = {"active": {"BOOL": True}}
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

    def finish_linux(self, event):
        result = auth.lambda_handler(event, None)
        if result["statusCode"] != 302 or result["headers"]["Location"] != "/desktop":
            return result
        callback = result
        result = auth.lambda_handler(self.session_event(callback, "POST /linux"), None)
        if result["statusCode"] != 200:
            return result
        self.assertIn('content="0;url=/linux"', result["body"])
        return auth.lambda_handler(self.session_event(callback, "GET /linux"), None)

    def session_event(self, callback, route, form=None):
        cookie = next(c for c in callback["cookies"] if c.startswith(auth.SESSION_COOKIE + "="))
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        session = self.state.sessions[auth.session_key(token)]
        return {
            "routeKey": route, "cookies": [cookie.split(";", 1)[0]],
            "headers": {"origin": os.environ["BASE_URL"], "content-type": "application/x-www-form-urlencoded"},
            "body": urlencode({"csrf": session["csrf"]["S"], **(form or {})}),
        }


class OAuthTests(AuthTestCase):
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

    def assert_centered_japanese_page(self, result):
        self.assertEqual(result["headers"]["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.assertIn('<html lang="ja">', result["body"])
        self.assertIn('name="viewport"', result["body"])
        self.assertIn('<link rel="icon" href="/favicon.ico" type="image/x-icon">', result["body"])
        self.assertIn(
            '<header><img src="/ops-frontier-logo.svg" alt="ops-frontier">'
            '<p class="service-title">ゼロトラスト仮想化端末サービス</p></header>',
            result["body"],
        )
        for rule in (
            "grid-template-columns:minmax(0,1fr) auto minmax(0,1fr)",
            "justify-self:start", "@media(max-width:64rem)",
            ".service-title{grid-row:2;}",
        ):
            self.assertIn(rule, result["body"])
        for rule in (
            "min-height:100vh", "display:flex", "align-items:center",
            "justify-content:center", "text-align:center",
        ):
            self.assertIn(rule, result["body"])
        nonce = result["body"].split('<style nonce="', 1)[1].split('"', 1)[0]
        self.assertEqual(
            result["headers"]["Content-Security-Policy"],
            f"default-src 'none'; img-src 'self'; style-src 'nonce-{nonce}'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'",
        )

    def test_form_action_allows_only_self(self):
        for region in ("ap-northeast-1", "us-west-2"):
            with self.subTest(region=region), patch.dict(os.environ, {"AWS_REGION": region}):
                result = auth.desktop_response({"csrf": {"S": "test-csrf"}})
                directives = result["headers"]["Content-Security-Policy"].split("; ")
                form_action = next(d for d in directives if d.startswith("form-action "))
                self.assertEqual(
                    form_action.split(),
                    ["form-action", "'self'"],
                )
                self.assertIn("base-uri 'none'", directives)
                self.assertIn('action="/linux"', result["body"])

    def test_static_assets_are_served_without_authentication(self):
        for filename, content_type in (
            ("favicon.ico", "image/x-icon"),
            ("ops-frontier-logo.svg", "image/svg+xml"),
        ):
            with self.subTest(filename=filename):
                result = auth.lambda_handler({"routeKey": f"GET /{filename}"}, None)
                self.assertEqual(result["statusCode"], 200)
                self.assertEqual(result["headers"]["Content-Type"], content_type)
                self.assertTrue(result["isBase64Encoded"])
                self.assertEqual(
                    base64.b64decode(result["body"], validate=True),
                    Path(__file__).with_name(filename).read_bytes(),
                )
                self.assertEqual(result["headers"]["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("cookies", result)
        self.secret.get_secret_value.assert_not_called()
        self.github.assert_not_called()
        self.assertEqual(self.state.items, {})

    def test_static_asset_routes_only_allow_known_get_requests(self):
        for route in (
            "POST /favicon.ico", "POST /ops-frontier-logo.svg",
            "GET /github_auth.py", "GET /../github_auth.py",
        ):
            with self.subTest(route=route):
                result = auth.lambda_handler({"routeKey": route}, None)
                self.assertEqual(result["statusCode"], 404)
                self.assert_centered_japanese_page(result)

    def test_session_ended_page_offers_login_without_starting_oauth(self):
        result = auth.lambda_handler({"routeKey": "GET /session-ended"}, None)
        self.assertEqual(result["statusCode"], 200)
        self.assert_centered_japanese_page(result)
        self.assertIn(
            '<main><h1>セッションを終了しました</h1>'
            '<p><a href="/login">再度ログインする</a></p></main>',
            result["body"],
        )
        self.assertNotIn("Location", result["headers"])
        self.assertNotIn('http-equiv="refresh"', result["body"])
        self.assertIn("Max-Age=0", result["cookies"][0])
        self.secret.get_secret_value.assert_not_called()
        self.github.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()
        self.assertEqual(self.state.items, {})

    def test_fleet_warming_page_is_japanese_and_preserves_retry(self):
        result = auth.fleet_warming_response()
        self.assertEqual(result["statusCode"], 503)
        self.assert_centered_japanese_page(result)
        self.assertIn("<h1>接続用リソースを準備しています</h1>", result["body"])
        self.assertIn("10〜20 分", result["body"])
        self.assertIn("20 秒ごと", result["body"])
        self.assertIn('<a href="/linux">もう一度確認する</a>', result["body"])
        self.assertIn('http-equiv="refresh" content="20;url=/linux"', result["body"])
        self.assertEqual(result["headers"]["Retry-After"], "20")
        self.assertIn("Max-Age=0", result["cookies"][0])

    def test_active_member_launches_with_stable_id_and_60_second_url(self):
        _, _, event = self.begin()
        with self.assertLogs(auth.LOGGER, level="INFO") as logs:
            result = self.finish_linux(event)
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
        result = self.finish_linux(event)
        self.appstream.describe_fleets.assert_called_once_with(Names=["test-fleet"])
        self.appstream.update_fleet.assert_called_once_with(
            Name="test-fleet", ComputeCapacity={"DesiredInstances": 1}
        )
        self.appstream.create_streaming_url.assert_not_called()
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(result["headers"]["Retry-After"], "20")
        self.assertIn('href="/linux"', result["body"])
        self.assertIn('http-equiv="refresh"', result["body"])
        self.assertNotIn("Location", result["headers"])
        self.assertIn("Max-Age=0", result["cookies"][0])

    def test_pending_fleet_waits_without_requesting_more_instances(self):
        fleet = self.appstream.describe_fleets.return_value["Fleets"][0]
        fleet["ComputeCapacityStatus"].update(
            Desired=1, Available=0, Running=0
        )
        _, _, event = self.begin()
        result = self.finish_linux(event)
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
                    {"access_token": "token"}, {"id": 123, "login": "user123"}, {"state": "active"}
                ]
                _, _, event = self.begin()
                result = self.finish_linux(event)
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
        self.assertEqual(self.finish_linux(event)["statusCode"], 302)
        self.appstream.update_fleet.assert_not_called()
        self.appstream.create_streaming_url.assert_called_once()

    def test_linux_post_ends_form_submission_before_external_navigation(self):
        _, _, event = self.begin()
        callback = auth.lambda_handler(event, None)
        result = auth.lambda_handler(self.session_event(callback, "POST /linux"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assert_centered_japanese_page(result)
        self.assertNotIn("Location", result["headers"])
        self.assertIn('http-equiv="refresh" content="0;url=/linux"', result["body"])
        self.assertIn('<a href="/linux">接続を続ける</a>', result["body"])
        self.appstream.describe_fleets.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()
        result = auth.lambda_handler(self.session_event(callback, "GET /linux"), None)
        self.assertEqual(result["statusCode"], 302)
        self.assertEqual(result["headers"]["Location"], "https://stream.example.com/session")

    def test_linux_waiting_poll_resumes_without_oauth_or_selection(self):
        _, _, event = self.begin()
        callback = auth.lambda_handler(event, None)
        capacity = self.appstream.describe_fleets.return_value["Fleets"][0]["ComputeCapacityStatus"]
        capacity.update(Desired=1, Available=0, InUse=0)
        result = auth.lambda_handler(self.session_event(callback, "POST /linux"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn('content="0;url=/linux"', result["body"])
        self.assertNotIn("Location", result["headers"])
        result = auth.lambda_handler(self.session_event(callback, "GET /linux"), None)
        self.assertEqual(result["statusCode"], 503)
        self.assertIn('content="20;url=/linux"', result["body"])
        capacity["Available"] = 1
        result = auth.lambda_handler(self.session_event(callback, "GET /linux"), None)
        self.assertEqual(result["headers"]["Location"], "https://stream.example.com/session")
        self.assertEqual(self.github.call_count, 3)

    def test_missing_or_stopped_fleet_is_not_started(self):
        for fleets in ([], [{"State": "STOPPED"}], [{"State": "STARTING"}]):
            with self.subTest(fleets=fleets):
                self.appstream.describe_fleets.return_value = {"Fleets": fleets}
                self.github.side_effect = [
                    {"access_token": "token"}, {"id": 123, "login": "user123"}, {"state": "active"}
                ]
                _, _, event = self.begin()
                result = self.finish_linux(event)
                self.assertEqual(result["statusCode"], 503)
                self.assertIn("管理者", result["body"])
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
                    {"access_token": "token"}, {"id": 123, "login": "user123"}, {"state": "active"}
                ]
                _, _, event = self.begin()
                with self.assertLogs(auth.LOGGER, level="ERROR") as logs:
                    result = self.finish_linux(event)
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
        self.assertEqual(self.finish_linux(event)["statusCode"], 302)
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
        result = self.finish_linux(event)
        self.assertEqual(result["statusCode"], 503)
        self.assertNotIn("Location", result["headers"])
        self.assertIn("Max-Age=0", result["cookies"][0])

    def test_unknown_route_is_not_found(self):
        self.assertEqual(
            auth.lambda_handler({"routeKey": "POST /callback"}, None)["statusCode"], 404
        )
        self.secret.get_secret_value.assert_not_called()


class WindowsTests(AuthTestCase):
    def login(self, login="changeable-name", user_id=123456):
        self.github.side_effect = [
            {"access_token": "never-log-token"}, {"id": user_id, "login": login},
            {"state": "active"},
        ]
        _, _, event = self.begin()
        callback = auth.lambda_handler(event, None)
        self.assertEqual(callback["headers"]["Location"], "/desktop")
        return callback

    def owned_user(self):
        return {
            "Enabled": True,
            "OtherAttributes": {"Description": {"S": "bustion-github:123456"}},
        }

    def start(self, callback, password="ValidTestPassword!42"):
        return auth.lambda_handler(
            self.session_event(callback, "POST /windows", {"password": password, "confirm": password}),
            None,
        )

    def registered(self, callback):
        self.ds_data.describe_user.return_value = self.owned_user()
        self.state.users.update({
            "github:123456": {
                "principal": {"S": "github:123456"}, "user_id": {"S": "123456"},
                "login": {"S": "changeable-name"}, "ad_ready": {"BOOL": True},
            },
            "login:changeable-name": {
                "principal": {"S": "login:changeable-name"}, "user_id": {"S": "123456"},
            },
        })

    def missing_directory_user(self):
        self.ds_data.describe_user.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeUser"
        )

    def test_oauth_displays_choice_without_provisioning(self):
        callback = self.login()
        result = auth.lambda_handler(self.session_event(callback, "GET /desktop"), None)
        self.assertEqual(result["statusCode"], 200)
        for text in (
            "セキュアブラウザを使用する", "パーソナルデスクトップを使用する",
            auth.CLIENT_DOWNLOAD, 'action="/linux"', 'action="/windows"',
        ):
            self.assertIn(text, result["body"])
        for old_name in ("AWA Linux", "AWP Windows"):
            self.assertNotIn(old_name, result["body"])
        self.ds_data.create_user.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()
        self.appstream.create_streaming_url.assert_not_called()
        self.assertNotIn("never-log", repr(self.state.sessions))

    def test_new_user_is_prompted_without_creating_any_resource(self):
        callback = self.login()
        result = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("<h1>パーソナルデスクトップの初回登録</h1>", result["body"])
        self.assertIn('type="password"', result["body"])
        self.assertIn('name="confirm"', result["body"])
        self.assertIn("変更しない", result["body"])
        self.assertEqual(self.state.users, {})
        self.ds_data.create_user.assert_not_called()

    def test_personal_desktop_waiting_and_connection_labels(self):
        result = auth.windows_waiting_response()
        self.assertIn("<title>パーソナルデスクトップを準備しています</title>", result["body"])
        self.assertIn("<h1>パーソナルデスクトップを準備しています</h1>", result["body"])
        self.assertIn("20 秒ごと", result["body"])
        self.assertIn('http-equiv="refresh" content="20;url=/windows"', result["body"])
        self.workspaces.describe_workspaces.return_value = {
            "Workspaces": [{"State": "AVAILABLE"}],
        }
        self.workspaces.describe_workspace_directories.return_value = {
            "Directories": [{"RegistrationCode": "test-registration"}],
        }
        result = auth.workspace_response({"login": {"S": "test-user"}}, {})
        self.assertIn("<title>パーソナルデスクトップに接続</title>", result["body"])
        self.assertIn("<h1>パーソナルデスクトップに接続できます</h1>", result["body"])
        self.assertIn('href="workspaces://test-user@test-registration"', result["body"])

    def test_first_registration_and_creation_then_poll_and_launch(self):
        callback = self.login()
        self.missing_directory_user()
        with self.assertLogs(auth.LOGGER, level="INFO") as logs:
            result = self.start(callback)
        self.assertIn("準備しています", result["body"])
        self.ds_data.create_user.assert_called_once_with(
            DirectoryId="d-1234567890", SAMAccountName="changeable-name",
            ClientToken="github-123456",
            OtherAttributes={"description": {"S": "bustion-github:123456"}},
        )
        self.ds.reset_user_password.assert_called_once_with(
            DirectoryId="d-1234567890", UserName="changeable-name",
            NewPassword="ValidTestPassword!42",
        )
        request = self.workspaces.create_workspaces.call_args.kwargs["Workspaces"][0]
        self.assertEqual(request["BundleId"], "wsb-96cqc2ksn")
        self.assertEqual(request["WorkspaceProperties"], {
            "Protocols": ["WSP"], "RunningMode": "AUTO_STOP",
            "RunningModeAutoStopTimeoutInMinutes": 60,
        })
        self.assertNotIn("ValidTestPassword", repr(self.state.users) + repr(self.state.sessions) + repr(logs.output) + repr(result))
        self.ds_data.describe_user.side_effect = None
        self.ds_data.describe_user.return_value = self.owned_user()
        poll = self.session_event(callback, "GET /windows")
        auth.lambda_handler(poll, None)
        self.workspaces.create_workspaces.assert_called_once()
        self.workspaces.describe_workspaces.return_value = {
            "Workspaces": [{"WorkspaceId": "ws-123456789", "State": "PENDING"}],
        }
        self.assertIn('content="20;url=/windows"', auth.lambda_handler(poll, None)["body"])
        for state in ("AVAILABLE", "STOPPED"):
            self.workspaces.describe_workspaces.return_value["Workspaces"][0]["State"] = state
            self.workspaces.describe_workspace_directories.return_value = {
                "Directories": [{"RegistrationCode": "WSnrt+ABC12D"}],
            }
            result = auth.lambda_handler(poll, None)
            self.assertIn("workspaces://changeable-name@WSnrt%2BABC12D", result["body"])
            self.assertIn(auth.CLIENT_DOWNLOAD, result["body"])
        self.ds.reset_user_password.assert_called_once()

    def test_bad_password_and_confirmation_are_not_forwarded(self):
        callback = self.login()
        for password, confirm in (
            ("short", "short"), ("ValidTestPassword!42", "different"),
            ("a" * 128, "a" * 128), ("CHANGEABLE-NAME42!", "CHANGEABLE-NAME42!"),
        ):
            event = self.session_event(callback, "POST /windows", {"password": password, "confirm": confirm})
            self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 400)
        self.ds_data.create_user.assert_not_called()
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()
        self.assertEqual(self.state.users, {})

    def test_password_policy_failure_can_be_retried_without_recreating_user(self):
        callback = self.login()
        self.ds_data.describe_user.return_value = self.owned_user()
        self.ds.reset_user_password.side_effect = ClientError(
            {"Error": {"Code": "InvalidPasswordException", "Message": "secret"}}, "ResetUserPassword",
        )
        result = self.start(callback)
        self.assertEqual(result["statusCode"], 400)
        self.assertNotIn("secret", result["body"])
        self.workspaces.create_workspaces.assert_not_called()
        self.ds.reset_user_password.side_effect = None
        self.assertEqual(self.start(callback)["statusCode"], 200)
        self.ds_data.create_user.assert_not_called()
        self.assertTrue(self.state.users["github:123456"]["ad_ready"]["BOOL"])

    def test_windows_username_constraints_do_not_block_linux(self):
        for login in ("a" * 21, "Admin", "Administrator", "Guest", "krbtgt"):
            callback = self.login(login)
            result = auth.lambda_handler(self.session_event(callback, "POST /windows"), None)
            self.assertEqual(result["statusCode"], 400)
            self.assertEqual(auth.lambda_handler(self.session_event(callback, "POST /linux"), None)["statusCode"], 200)
            self.assertEqual(auth.lambda_handler(self.session_event(callback, "GET /linux"), None)["statusCode"], 302)
        self.assertEqual(self.state.users, {})
        self.assertEqual(
            auth.validate_windows_login({"login": {"S": "a" * 20}}), "a" * 20,
        )

    def test_renamed_or_reused_github_login_is_rejected(self):
        callback = self.login()
        self.registered(callback)
        renamed = self.login("new-name")
        self.assertEqual(self.start(renamed)["statusCode"], 403)
        reused = self.login(user_id=987654)
        self.assertEqual(self.start(reused)["statusCode"], 409)
        self.workspaces.create_workspaces.assert_not_called()
        self.ds.reset_user_password.assert_not_called()

    def test_unmanaged_directory_user_is_never_adopted_or_reset(self):
        callback = self.login()
        self.ds_data.describe_user.return_value = {"OtherAttributes": {}, "Enabled": True}
        self.assertEqual(self.start(callback)["statusCode"], 409)
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()

    def test_disabled_or_deleted_registered_user_is_not_reenabled(self):
        callback = self.login()
        self.registered(callback)
        self.ds_data.describe_user.return_value["Enabled"] = False
        self.assertEqual(self.start(callback)["statusCode"], 403)
        self.missing_directory_user()
        self.assertEqual(self.start(callback)["statusCode"], 403)
        self.ds.reset_user_password.assert_not_called()

    def test_concurrent_request_waits_instead_of_resetting_or_creating(self):
        callback = self.login()
        self.registered(callback)
        self.state.users["github:123456"]["lease_until"] = {"N": str(int(auth.time.time()) + 60)}
        result = self.start(callback)
        self.assertIn("準備しています", result["body"])
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()

    def test_existing_workspace_does_not_trigger_creation_or_password_reset(self):
        callback = self.login()
        self.registered(callback)
        self.workspaces.describe_workspaces.return_value = {
            "Workspaces": [{"WorkspaceId": "ws-123456789", "State": "AVAILABLE"}],
        }
        self.workspaces.describe_workspace_directories.return_value = {
            "Directories": [{"RegistrationCode": "WSnrt+ABC12D"}],
        }
        self.assertIn("workspaces://", self.start(callback)["body"])
        self.ds.reset_user_password.assert_not_called()
        self.workspaces.create_workspaces.assert_not_called()

    def test_user_replication_delay_retries_only_after_explicit_rejection(self):
        callback = self.login()
        self.registered(callback)
        self.workspaces.create_workspaces.return_value = {
            "FailedRequests": [{"ErrorCode": "ResourceNotFound.User"}],
        }
        self.assertIn("準備しています", self.start(callback)["body"])
        self.assertNotIn("workspace_requested", self.state.users["github:123456"])
        self.workspaces.create_workspaces.return_value = {
            "PendingRequests": [{"WorkspaceId": "ws-123456789"}],
        }
        auth.lambda_handler(self.session_event(callback, "GET /windows"), None)
        self.assertEqual(self.workspaces.create_workspaces.call_count, 2)

    def test_creation_rejection_is_an_error_and_is_not_retried(self):
        callback = self.login()
        self.registered(callback)
        self.workspaces.create_workspaces.return_value = {
            "FailedRequests": [{"ErrorCode": "ResourceLimitExceeded", "ErrorMessage": "sensitive"}],
        }
        self.assertEqual(self.start(callback)["statusCode"], 503)
        result = auth.lambda_handler(self.session_event(callback, "GET /windows"), None)
        self.assertEqual(result["statusCode"], 503)
        self.assertNotIn("sensitive", result["body"])
        self.workspaces.create_workspaces.assert_called_once()

    def test_create_api_exception_remains_explicit_on_subsequent_requests(self):
        callback = self.login()
        self.registered(callback)
        self.workspaces.create_workspaces.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "CreateWorkspaces",
        )
        self.assertEqual(self.start(callback)["statusCode"], 503)
        self.assertEqual(
            auth.lambda_handler(self.session_event(callback, "GET /windows"), None)["statusCode"], 503,
        )
        self.workspaces.create_workspaces.assert_called_once()

    def test_uncertain_transport_result_does_not_duplicate_creation(self):
        callback = self.login()
        self.registered(callback)
        self.workspaces.create_workspaces.side_effect = auth.BotoCoreError()
        self.assertEqual(self.start(callback)["statusCode"], 503)
        self.assertIn("workspace_requested", self.state.users["github:123456"])
        auth.lambda_handler(self.session_event(callback, "GET /windows"), None)
        self.workspaces.create_workspaces.assert_called_once()
        self.state.users["github:123456"]["workspace_requested"] = {"N": "1"}
        self.assertEqual(auth.lambda_handler(self.session_event(callback, "GET /windows"), None)["statusCode"], 503)

    def test_workspace_failure_states_and_missing_registration_code_are_errors(self):
        callback = self.login()
        self.registered(callback)
        for state in ("ERROR", "UNHEALTHY", "TERMINATED", "AVAILABLE"):
            self.workspaces.describe_workspaces.return_value = {"Workspaces": [{"State": state}]}
            self.workspaces.describe_workspace_directories.return_value = {"Directories": []}
            self.assertEqual(self.start(callback)["statusCode"], 503)
        self.workspaces.create_workspaces.assert_not_called()

    def test_session_csrf_origin_and_form_validation(self):
        callback = self.login()
        valid = self.session_event(callback, "POST /windows")
        for event in (
            {**valid, "cookies": []},
            {**valid, "headers": {**valid["headers"], "origin": "https://evil.example"}},
            {**valid, "body": urlencode({"csrf": "wrong"})},
            {**valid, "body": valid["body"] + "&csrf=second"},
            {**valid, "body": "%invalid-base64", "isBase64Encoded": True},
            {**valid, "body": "x" * 8193},
        ):
            self.assertIn(auth.lambda_handler(event, None)["statusCode"], (400, 403))
        self.assertEqual(self.state.users, {})
        session = next(iter(self.state.sessions.values()))
        session["expires_at"] = {"N": "0"}
        self.assertEqual(auth.lambda_handler(valid, None)["statusCode"], 403)

    def test_form_pages_keep_origin_for_same_origin_posts(self):
        callback = self.login()
        page = auth.lambda_handler(self.session_event(callback, "GET /desktop"), None)
        self.assertEqual(page["headers"]["Referrer-Policy"], "same-origin")
        self.assertIn('action="/windows"', page["body"])
        valid = self.session_event(callback, "POST /windows")
        null_origin = {**valid, "headers": {**valid["headers"], "origin": "null"}}
        self.assertEqual(auth.lambda_handler(null_origin, None)["statusCode"], 403)

    def test_base64_form_is_accepted(self):
        callback = self.login()
        event = self.session_event(callback, "POST /windows")
        event.update(body=base64.b64encode(event["body"].encode()).decode(), isBase64Encoded=True)
        self.assertEqual(auth.lambda_handler(event, None)["statusCode"], 200)

    def test_direct_get_does_not_provision_before_selecting_windows(self):
        callback = self.login()
        for route in ("GET /windows", "GET /linux"):
            result = auth.lambda_handler(self.session_event(callback, route), None)
            self.assertEqual(result["headers"]["Location"], "/desktop")
        self.assertEqual(self.state.users, {})


class WindowsApiTests(unittest.TestCase):
    def client(self, service):
        return auth.boto3.client(
            service, region_name="ap-northeast-1",
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )

    def test_directory_registration_matches_sdk_models(self):
        data, directory = self.client("ds-data"), self.client("ds")
        session = {"user_id": {"S": "123456"}, "login": {"S": "test-user"}}
        with (
            Stubber(data) as data_stub, Stubber(directory) as directory_stub,
            patch.object(auth, "aws_client", side_effect={"ds-data": data, "ds": directory}.__getitem__),
            patch.object(auth, "update_user") as update,
            patch.dict(os.environ, {"DIRECTORY_ID": "d-1234567890"}),
        ):
            data_stub.add_client_error(
                "describe_user", service_error_code="ResourceNotFoundException",
                expected_params={"DirectoryId": "d-1234567890", "SAMAccountName": "test-user", "OtherAttributes": ["description"]},
            )
            data_stub.add_response(
                "create_user", {"SID": "S-1-5-21-1234567890-1234567890-1234567890-1001"},
                {"DirectoryId": "d-1234567890", "SAMAccountName": "test-user", "ClientToken": "github-123456",
                 "OtherAttributes": {"description": {"S": "bustion-github:123456"}}},
            )
            directory_stub.add_response(
                "reset_user_password", {},
                {"DirectoryId": "d-1234567890", "UserName": "test-user", "NewPassword": "ValidPassword!42"},
            )
            self.assertIsNone(auth.register_directory_user(session, {"password": "ValidPassword!42", "confirm": "ValidPassword!42"}))
            update.assert_called_once_with(session, "SET ad_ready = :ready", {":ready": {"BOOL": True}})
            data_stub.assert_no_pending_responses()
            directory_stub.assert_no_pending_responses()

    def test_workspace_request_matches_sdk_models(self):
        client = self.client("workspaces")
        session = {"user_id": {"S": "123456"}, "login": {"S": "test-user"}}
        request = {
            "DirectoryId": "d-1234567890", "UserName": "test-user",
            "BundleId": "wsb-96cqc2ksn",
            "WorkspaceProperties": {
                "Protocols": ["WSP"], "RunningMode": "AUTO_STOP",
                "RunningModeAutoStopTimeoutInMinutes": 60,
            },
            "Tags": [{"Key": "GitHubUserId", "Value": "123456"}],
        }
        with (
            Stubber(client) as stub, patch.object(auth, "aws_client", return_value=client),
            patch.object(auth, "update_user"), patch.dict(os.environ, {
                "AWS_REGION": "ap-northeast-1",
                "DIRECTORY_ID": "d-1234567890", "WORKSPACE_BUNDLE_ID": "wsb-96cqc2ksn",
            }),
        ):
            stub.add_response(
                "describe_workspaces", {"Workspaces": []},
                {"DirectoryId": "d-1234567890", "UserName": "test-user"},
            )
            stub.add_response(
                "create_workspaces",
                {"FailedRequests": [], "PendingRequests": [{"WorkspaceId": "ws-123456789", "State": "PENDING"}]},
                {"Workspaces": [request]},
            )
            self.assertIn("準備しています", auth.workspace_response(session, {})["body"])
            stub.assert_no_pending_responses()


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
