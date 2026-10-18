import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
COOKIE_NAME = "__Host-oauth-state"
STATE_LIFETIME = 600
AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})


class AuthError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(code)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def aws_client(service):
    return boto3.client(service, config=AWS_CONFIG)


def response(status, body="", location=None, cookie=None):
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Strict-Transport-Security": "max-age=31536000",
    }
    if location is not None:
        headers["Location"] = location
    result = {"statusCode": status, "headers": headers, "body": body}
    if cookie is not None:
        result["cookies"] = [
            f"{COOKIE_NAME}={cookie}; Secure; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={STATE_LIFETIME if cookie else 0}"
        ]
    return result


def github_json(url, token=None, form=None, membership=False):
    headers = {"Accept": "application/json", "User-Agent": "aws-bustion-oauth"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = None
    if form is not None:
        data = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with build_opener(NoRedirects()).open(
            Request(url, data=data, headers=headers), timeout=3
        ) as upstream:
            payload = json.load(upstream)
    except HTTPError as error:
        error.close()
        if membership and error.code == 404:
            raise AuthError(
                403, "not_member", "Active organization membership is required."
            ) from error
        raise AuthError(
            502, f"github_http_{error.code}", "GitHub request failed. Please sign in again."
        ) from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise AuthError(
            502, "github_unavailable", "GitHub request failed. Please sign in again."
        ) from error
    if not isinstance(payload, dict):
        raise AuthError(502, "github_invalid_response", "Invalid response from GitHub.")
    return payload


def oauth_credentials():
    result = aws_client("secretsmanager").get_secret_value(
        SecretId=os.environ["OAUTH_SECRET_ARN"]
    )
    try:
        credentials = json.loads(result.get("SecretString", ""))
    except ValueError as error:
        raise AuthError(
            503, "oauth_not_configured", "GitHub OAuth credentials are not configured."
        ) from error
    if not isinstance(credentials, dict) or any(
        not isinstance(credentials.get(key), str) or not credentials[key].strip()
        for key in ("client_id", "client_secret")
    ):
        raise AuthError(
            503, "oauth_not_configured", "GitHub OAuth credentials are not configured."
        )
    return credentials


def start_login():
    credentials = oauth_credentials()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    aws_client("dynamodb").put_item(
        TableName=os.environ["STATE_TABLE"],
        Item={
            "state_id": {"S": hashlib.sha256(state.encode("ascii")).hexdigest()},
            "verifier": {"S": verifier},
            "expires_at": {"N": str(int(time.time()) + STATE_LIFETIME)},
        },
        ConditionExpression="attribute_not_exists(state_id)",
    )
    query = urlencode(
        {
            "client_id": credentials["client_id"],
            "redirect_uri": f"{os.environ['BASE_URL']}/callback",
            "scope": "read:org",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return response(
        302, location=f"https://github.com/login/oauth/authorize?{query}", cookie=state
    )


def consume_state(event):
    params = parse_qs(event.get("rawQueryString", ""), keep_blank_values=True)
    state_values = params.get("state", [])
    cookie = SimpleCookie()
    try:
        cookie.load("; ".join(event.get("cookies", [])))
    except CookieError as error:
        raise AuthError(400, "invalid_state", "Invalid sign-in state. Start again.") from error
    cookie_state = cookie.get(COOKIE_NAME)
    if (
        len(state_values) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state_values[0])
        or cookie_state is None
        or not hmac.compare_digest(
            state_values[0].encode("utf-8"), cookie_state.value.encode("utf-8")
        )
    ):
        raise AuthError(400, "invalid_state", "Invalid sign-in state. Start again.")
    try:
        result = aws_client("dynamodb").delete_item(
            TableName=os.environ["STATE_TABLE"],
            Key={
                "state_id": {
                    "S": hashlib.sha256(state_values[0].encode("ascii")).hexdigest()
                }
            },
            ConditionExpression="expires_at > :now",
            ExpressionAttributeValues={":now": {"N": str(int(time.time()))}},
            ReturnValues="ALL_OLD",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AuthError(
                400, "expired_state", "Sign-in expired or already used. Start again."
            ) from error
        raise
    return params, result["Attributes"]["verifier"]["S"]


def ensure_fleet_capacity(appstream):
    fleet_name = os.environ["APPSTREAM_FLEET"]
    fleets = appstream.describe_fleets(Names=[fleet_name])["Fleets"]
    if len(fleets) != 1 or fleets[0]["State"] != "RUNNING":
        raise AuthError(
            503, "fleet_unavailable", "The fleet is not running. Contact the administrator."
        )
    capacity = fleets[0]["ComputeCapacityStatus"]
    # Preserve reconnection to existing sessions even if no spare instance exists.
    if capacity["InUse"] > 0:
        return True
    if capacity["Desired"] == 0:
        appstream.update_fleet(
            Name=fleet_name, ComputeCapacity={"DesiredInstances": 1}
        )
        LOGGER.info("Requested first AppStream instance for fleet %s", fleet_name)
        return False
    return capacity["Available"] > 0


def fleet_warming_response():
    LOGGER.info("AppStream fleet is provisioning; deferring streaming URL creation")
    result = response(
        503,
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="60;url=/login">'
        "<title>Preparing streaming resources</title></head><body>"
        "<h1>Preparing streaming resources</h1>"
        "<p>The fleet is starting. Provisioning usually takes 10-20 minutes. "
        "This page checks again every 60 seconds via GitHub sign-in.</p>"
        '<p><a href="/login">Check again</a></p>'
        "<p>Do not refresh the OAuth callback URL or use AppStream Retry. "
        "If this continues, contact the administrator.</p></body></html>",
        cookie="",
    )
    result["headers"]["Content-Type"] = "text/html; charset=utf-8"
    result["headers"]["Retry-After"] = "60"
    return result


def finish_login(event):
    params, verifier = consume_state(event)
    if "error" in params:
        raise AuthError(400, "oauth_denied", "GitHub authorization was not completed.")
    codes = params.get("code", [])
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 1024:
        raise AuthError(400, "invalid_code", "Missing or invalid authorization code.")
    credentials = oauth_credentials()
    token_response = github_json(
        "https://github.com/login/oauth/access_token",
        form={
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "code": codes[0],
            "redirect_uri": f"{os.environ['BASE_URL']}/callback",
            "code_verifier": verifier,
        },
    )
    token = token_response.get("access_token")
    if "error" in token_response or not isinstance(token, str) or not token:
        raise AuthError(400, "token_exchange_failed", "GitHub sign-in failed. Start again.")
    user = github_json("https://api.github.com/user", token=token)
    user_id = user.get("id")
    if type(user_id) is not int or user_id <= 0 or len(f"gh-{user_id}") > 32:
        raise AuthError(502, "invalid_user", "Invalid GitHub user identifier.")
    membership = github_json(
        f"https://api.github.com/user/memberships/orgs/{os.environ['GITHUB_ORGANIZATION']}",
        token=token,
        membership=True,
    )
    if membership.get("state") != "active":
        raise AuthError(403, "not_active_member", "Active organization membership is required.")
    appstream = aws_client("appstream")
    if not ensure_fleet_capacity(appstream):
        return fleet_warming_response()
    result = appstream.create_streaming_url(
        StackName=os.environ["APPSTREAM_STACK"],
        FleetName=os.environ["APPSTREAM_FLEET"],
        UserId=f"gh-{user_id}",
        Validity=60,
    )
    LOGGER.info("AppStream launch authorized for GitHub user ID %s", user_id)
    return response(302, location=result["StreamingURL"], cookie="")


def lambda_handler(event, context):
    try:
        route = event.get("routeKey")
        if route in ("GET /", "GET /login"):
            return start_login()
        if route == "GET /callback":
            return finish_login(event)
        return response(404, "Not found.")
    except AuthError as error:
        LOGGER.warning("Authentication rejected: %s", error.code)
        return response(error.status, error.message, cookie="")
    except (ClientError, BotoCoreError) as error:
        code = (
            error.response["Error"]["Code"]
            if isinstance(error, ClientError)
            else type(error).__name__
        )
        LOGGER.error("AWS authentication dependency failed: %s", code)
        return response(
            503, "Sign-in service unavailable. Contact the administrator or try again.", cookie=""
        )
