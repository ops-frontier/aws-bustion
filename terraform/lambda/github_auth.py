import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from html import escape
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
COOKIE_NAME = "__Host-oauth-state"
STATE_LIFETIME = 600
SESSION_COOKIE = "__Host-bustion-session"
SESSION_LIFETIME = 3600
CLIENT_DOWNLOAD = "https://clients.amazonworkspaces.com/"
AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
# Directory and WorkSpaces writes can exceed 3 seconds; three calls still fit the 29-second Lambda timeout.
SLOW_AWS_CONFIG = Config(connect_timeout=2, read_timeout=7, retries={"total_max_attempts": 1})
SLOW_SERVICES = {"ds", "ds-data", "workspaces"}
STATIC_ASSETS = {
    "GET /favicon.ico": ("favicon.ico", "image/x-icon"),
    "GET /ops-frontier-logo.svg": ("ops-frontier-logo.svg", "image/svg+xml"),
}


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
    return boto3.client(
        service, config=SLOW_AWS_CONFIG if service in SLOW_SERVICES else AWS_CONFIG
    )


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
                403, "not_member", "指定された GitHub 組織の有効なメンバーである必要があります。"
            ) from error
        raise AuthError(
            502, f"github_http_{error.code}", "GitHub への通信に失敗しました。ログインし直してください。"
        ) from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise AuthError(
            502, "github_unavailable", "GitHub への通信に失敗しました。ログインし直してください。"
        ) from error
    if not isinstance(payload, dict):
        raise AuthError(502, "github_invalid_response", "GitHub からの応答が不正です。")
    return payload


def oauth_credentials():
    result = aws_client("secretsmanager").get_secret_value(
        SecretId=os.environ["OAUTH_SECRET_ARN"]
    )
    try:
        credentials = json.loads(result.get("SecretString", ""))
    except ValueError as error:
        raise AuthError(
            503, "oauth_not_configured", "GitHub OAuth の資格情報が設定されていません。管理者にお問い合わせください。"
        ) from error
    if not isinstance(credentials, dict) or any(
        not isinstance(credentials.get(key), str) or not credentials[key].strip()
        for key in ("client_id", "client_secret")
    ):
        raise AuthError(
            503, "oauth_not_configured", "GitHub OAuth の資格情報が設定されていません。管理者にお問い合わせください。"
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
        raise AuthError(400, "invalid_state", "ログイン状態を確認できません。ログインし直してください。") from error
    cookie_state = cookie.get(COOKIE_NAME)
    if (
        len(state_values) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state_values[0])
        or cookie_state is None
        or not hmac.compare_digest(
            state_values[0].encode("utf-8"), cookie_state.value.encode("utf-8")
        )
    ):
        raise AuthError(400, "invalid_state", "ログイン状態を確認できません。ログインし直してください。")
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
                400, "expired_state", "ログイン状態は期限切れ、または使用済みです。ログインし直してください。"
            ) from error
        raise
    return params, result["Attributes"]["verifier"]["S"]


def ensure_fleet_capacity(appstream):
    fleet_name = os.environ["APPSTREAM_FLEET"]
    fleets = appstream.describe_fleets(Names=[fleet_name])["Fleets"]
    if len(fleets) != 1 or fleets[0]["State"] != "RUNNING":
        raise AuthError(
            503, "fleet_unavailable", "セキュアブラウザのフリートが稼働していません。管理者にお問い合わせください。"
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


def page_response(status, title, body, head=""):
    nonce = secrets.token_urlsafe(16)
    result = response(
        status,
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{head}<title>{title}</title>"
        '<link rel="icon" href="/favicon.ico" type="image/x-icon">'
        f'<style nonce="{nonce}">'
        "body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;text-align:center;font-family:sans-serif;}"
        "header{box-sizing:border-box;width:100%;display:grid;"
        "grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);"
        "align-items:center;gap:1rem;padding:1rem;}"
        "header img{width:12rem;max-width:100%;height:auto;justify-self:start;}"
        ".service-title{margin:0;font-size:1.5rem;font-weight:bold;}"
        "main{box-sizing:border-box;max-width:48rem;padding:2rem;margin:auto;}"
        "h1{font-size:1.5rem;}p{line-height:1.8;}"
        "@media(max-width:64rem){header{grid-template-columns:minmax(0,1fr);}"
        ".service-title{grid-row:2;}}"
        "</style></head><body><header>"
        '<img src="/ops-frontier-logo.svg" alt="ops-frontier">'
        '<p class="service-title">ゼロトラスト仮想化端末サービス</p>'
        f"</header><main>{body}</main></body></html>",
        cookie="",
    )
    result["headers"]["Content-Type"] = "text/html; charset=utf-8"
    # Browsers send "Origin: null" on form POSTs from no-referrer pages, which fails the origin check.
    result["headers"]["Referrer-Policy"] = "same-origin"
    result["headers"]["Content-Security-Policy"] = (
        f"default-src 'none'; img-src 'self'; style-src 'nonce-{nonce}'; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'"
    )
    return result


def session_ended_response():
    return page_response(
        200,
        "セッションを終了しました",
        "<h1>セッションを終了しました</h1>"
        '<p><a href="/login">再度ログインする</a></p>',
    )


def fleet_warming_response():
    LOGGER.info("AppStream fleet is provisioning; deferring streaming URL creation")
    result = page_response(
        503,
        "接続用リソースを準備しています",
        "<h1>接続用リソースを準備しています</h1>"
        "<p>接続用の端末を起動しています。準備には通常 10〜20 分かかります。"
        "このページは 20 秒ごとに準備状況を確認します。</p>"
        '<p><a href="/linux">もう一度確認する</a></p>'
        "<p>OAuth コールバック URL の再読み込みや AppStream の Retry は使用しないでください。"
        "長時間この状態が続く場合は、管理者にお問い合わせください。</p>",
        head='<meta http-equiv="refresh" content="20;url=/linux">',
    )
    result["headers"]["Retry-After"] = "20"
    return result


def finish_login(event):
    params, verifier = consume_state(event)
    if "error" in params:
        raise AuthError(400, "oauth_denied", "GitHub の認可が完了していません。")
    codes = params.get("code", [])
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 1024:
        raise AuthError(400, "invalid_code", "認可コードがないか、不正な形式です。")
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
        raise AuthError(400, "token_exchange_failed", "GitHub 認証に失敗しました。ログインし直してください。")
    user = github_json("https://api.github.com/user", token=token)
    user_id = user.get("id")
    if type(user_id) is not int or user_id <= 0 or len(f"gh-{user_id}") > 32:
        raise AuthError(502, "invalid_user", "GitHub のユーザー ID が不正です。")
    membership = github_json(
        f"https://api.github.com/user/memberships/orgs/{os.environ['GITHUB_ORGANIZATION']}",
        token=token,
        membership=True,
    )
    if membership.get("state") != "active":
        raise AuthError(403, "not_active_member", "指定された GitHub 組織の有効なメンバーである必要があります。")
    login = user.get("login")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", login):
        raise AuthError(502, "invalid_login", "GitHub のユーザー名を取得できませんでした。")
    session_token = secrets.token_urlsafe(32)
    aws_client("dynamodb").put_item(
        TableName=os.environ["STATE_TABLE"],
        Item={
            "state_id": {"S": session_key(session_token)},
            "user_id": {"S": str(user_id)},
            "login": {"S": login.lower()},
            "csrf": {"S": secrets.token_urlsafe(32)},
            "expires_at": {"N": str(int(time.time()) + SESSION_LIFETIME)},
        },
        ConditionExpression="attribute_not_exists(state_id)",
    )
    result = response(302, location="/desktop", cookie="")
    result["cookies"].append(
        f"{SESSION_COOKIE}={session_token}; Secure; HttpOnly; SameSite=Lax; "
        f"Path=/; Max-Age={SESSION_LIFETIME}"
    )
    return result


def launch_linux(session):
    appstream = aws_client("appstream")
    if not ensure_fleet_capacity(appstream):
        return fleet_warming_response()
    result = appstream.create_streaming_url(
        StackName=os.environ["APPSTREAM_STACK"],
        FleetName=os.environ["APPSTREAM_FLEET"],
        UserId=f"gh-{session['user_id']['S']}",
        Validity=60,
    )
    LOGGER.info("AppStream launch authorized for GitHub user ID %s", session["user_id"]["S"])
    return response(302, location=result["StreamingURL"], cookie="")


def session_key(token):
    return "session:" + hashlib.sha256(token.encode("ascii")).hexdigest()


def authenticated_session(event):
    cookie = SimpleCookie()
    try:
        cookie.load("; ".join(event.get("cookies", [])))
    except CookieError as error:
        raise AuthError(403, "invalid_session", "ログインし直してください。") from error
    value = cookie.get(SESSION_COOKIE)
    if value is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value.value):
        raise AuthError(403, "invalid_session", "ログインし直してください。")
    key = session_key(value.value)
    session = aws_client("dynamodb").get_item(
        TableName=os.environ["STATE_TABLE"],
        Key={"state_id": {"S": key}},
        ConsistentRead=True,
    ).get("Item")
    if session is None or int(session["expires_at"]["N"]) <= int(time.time()):
        raise AuthError(403, "expired_session", "ログインの有効期限が切れました。")
    return key, session


def posted_form(event, session):
    headers = {key.lower(): value for key, value in event.get("headers", {}).items()}
    if headers.get("origin") != os.environ["BASE_URL"]:
        raise AuthError(403, "invalid_origin", "送信元を確認できません。ログインし直してください。")
    if headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise AuthError(400, "invalid_form", "フォームの形式が不正です。")
    body = event.get("body", "")
    if len(body) > 8192:
        raise AuthError(400, "large_form", "入力が長すぎます。")
    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        form = parse_qs(body, keep_blank_values=True, max_num_fields=8)
    except (ValueError, UnicodeError) as error:
        raise AuthError(400, "invalid_form", "フォームの形式が不正です。") from error
    if any(len(values) != 1 for values in form.values()):
        raise AuthError(400, "duplicate_form", "フォームの項目が重複しています。")
    if not hmac.compare_digest(
        form.get("csrf", [""])[0].encode(), session["csrf"]["S"].encode()
    ):
        raise AuthError(403, "invalid_csrf", "送信を確認できません。ログインし直してください。")
    return {key: values[0] for key, values in form.items()}


def csrf_field(session):
    return f'<input type="hidden" name="csrf" value="{escape(session["csrf"]["S"], quote=True)}">'


def desktop_response(session):
    return page_response(
        200, "利用する端末を選択",
        "<h1>利用する端末を選択してください</h1>"
        f'<form method="post" action="/linux">{csrf_field(session)}'
        '<p><button type="submit">セキュアブラウザを使用する</button></p></form>'
        f'<form method="post" action="/windows">{csrf_field(session)}'
        '<p><button type="submit">パーソナルデスクトップを使用する</button></p></form>'
        f'<p>パーソナルデスクトップを使用する前に <a href="{CLIENT_DOWNLOAD}">'
        "WorkSpaces DCV クライアントをインストール</a>してください。</p>",
    )


def validate_windows_login(session):
    login = session["login"]["S"]
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,18}[a-z0-9])?", login)
        or login in {"admin", "administrator", "guest", "krbtgt"}
    ):
        raise AuthError(
            400, "unsupported_windows_login",
            "パーソナルデスクトップを使用する GitHub ユーザー名は 20 文字以下で、"
            "Admin、Administrator、Guest、krbtgt 以外である必要があります。",
        )
    return login


def user_record(session):
    return aws_client("dynamodb").get_item(
        TableName=os.environ["USERS_TABLE"],
        Key={"principal": {"S": "github:" + session["user_id"]["S"]}},
        ConsistentRead=True,
    ).get("Item")


def claim_user(session):
    db = aws_client("dynamodb")
    user_id, login = session["user_id"]["S"], validate_windows_login(session)
    item = {
        "principal": {"S": "github:" + user_id},
        "login": {"S": login},
        "user_id": {"S": user_id},
    }
    try:
        db.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": os.environ["USERS_TABLE"],
                "Item": item,
                "ConditionExpression": "attribute_not_exists(principal)",
            }},
            {"Put": {
                "TableName": os.environ["USERS_TABLE"],
                "Item": {"principal": {"S": "login:" + login}, "user_id": {"S": user_id}},
                "ConditionExpression": "attribute_not_exists(principal)",
            }},
        ])
    except ClientError as error:
        if error.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = error.response.get("CancellationReasons", [])
        if not any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
            raise
        existing = user_record(session)
        if existing is None or existing["login"]["S"] != login:
            raise AuthError(
                409, "login_already_claimed",
                "このユーザー名は別の登録に使用されています。管理者にお問い合わせください。",
            ) from error
        return existing
    return item


def update_user(session, expression, values):
    return aws_client("dynamodb").update_item(
        TableName=os.environ["USERS_TABLE"],
        Key={"principal": {"S": "github:" + session["user_id"]["S"]}},
        UpdateExpression=expression,
        ExpressionAttributeValues=values,
    )


def password_response(session, message="", status=200):
    return page_response(
        status, "パーソナルデスクトップの初回登録",
        "<h1>パーソナルデスクトップの初回登録</h1>"
        f"<p>ユーザー名: {escape(session['login']['S'])}</p>"
        "<p>登録後は GitHub のユーザー名を変更しないでください。"
        "変更された場合、パーソナルデスクトップの利用を拒否します。</p>"
        f"<p>{escape(message)}</p>"
        f'<form method="post" action="/windows">{csrf_field(session)}'
        '<p><label>パスワード <input type="password" name="password" '
        'required minlength="8" maxlength="127" autocomplete="new-password"></label></p>'
        '<p><label>パスワードの確認 <input type="password" name="confirm" '
        'required minlength="8" maxlength="127" autocomplete="new-password"></label></p>'
        "<p>8〜127 文字で、英大文字・英小文字・数字・記号のうち 3 種類以上を使用し、"
        "ユーザー名を含めないでください。ディレクトリのポリシーも適用されます。</p>"
        '<button type="submit">登録してパーソナルデスクトップを準備する</button></form>',
    )


def validate_password(form, login):
    password = form.get("password", "")
    if password != form.get("confirm"):
        return "パスワードと確認用パスワードが一致しません。"
    categories = sum((
        bool(re.search(r"[A-Z]", password)), bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[0-9]", password)), bool(re.search(r"[^A-Za-z0-9]", password)),
    ))
    if not 8 <= len(password) <= 127 or categories < 3 or login in password.lower():
        return "パスワードが入力条件を満たしていません。"
    return ""


def directory_user(session):
    try:
        user = aws_client("ds-data").describe_user(
            DirectoryId=os.environ["DIRECTORY_ID"], SAMAccountName=session["login"]["S"],
            OtherAttributes=["description"],
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise
    attributes = {key.lower(): value for key, value in user.get("OtherAttributes", {}).items()}
    marker = attributes.get("description", {}).get("S")
    if marker != "bustion-github:" + session["user_id"]["S"]:
        raise AuthError(
            409, "unmanaged_directory_user",
            "同名のディレクトリユーザーが存在し、所有者を確認できません。管理者にお問い合わせください。",
        )
    return user


def register_directory_user(session, form):
    message = validate_password(form, session["login"]["S"])
    if message:
        return password_response(session, message, 400)
    user = directory_user(session)
    if user is None:
        # A stable token also prevents duplicate creation after an uncertain API result.
        aws_client("ds-data").create_user(
            DirectoryId=os.environ["DIRECTORY_ID"], SAMAccountName=session["login"]["S"],
            ClientToken="github-" + session["user_id"]["S"],
            OtherAttributes={"description": {"S": "bustion-github:" + session["user_id"]["S"]}},
        )
    try:
        aws_client("ds").reset_user_password(
            DirectoryId=os.environ["DIRECTORY_ID"], UserName=session["login"]["S"],
            NewPassword=form["password"],
        )
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in {"InvalidPasswordException", "UserDoesNotExistException"}:
            LOGGER.warning("Directory password setup rejected: %s", code)
            return password_response(
                session, "パスワードの設定に失敗しました。"
                "ディレクトリへの反映を少し待ち、ポリシーを満たすパスワードで再送信してください。", 400,
            )
        raise
    update_user(session, "SET ad_ready = :ready", {":ready": {"BOOL": True}})
    LOGGER.info("Directory registration completed for GitHub user ID %s", session["user_id"]["S"])
    return None


def windows_waiting_response():
    return page_response(
        200, "パーソナルデスクトップを準備しています",
        "<h1>パーソナルデスクトップを準備しています</h1>"
        "<p>WorkSpace の作成と起動には10分程度の時間がかかります。20 秒ごとに状況を確認します。</p>"
        '<p><a href="/windows">もう一度確認する</a></p>'
        '<p>長時間完了しない場合は管理者にお問い合わせください。</p>',
        head='<meta http-equiv="refresh" content="20;url=/windows">',
    )


def workspace_response(session, record):
    client = aws_client("workspaces")
    found = client.describe_workspaces(
        DirectoryId=os.environ["DIRECTORY_ID"], UserName=session["login"]["S"]
    ).get("Workspaces", [])
    if len(found) > 1:
        raise AuthError(409, "multiple_workspaces", "複数の WorkSpace が存在します。管理者にお問い合わせください。")
    if found:
        workspace = found[0]
        state = workspace["State"]
        if state in {"AVAILABLE", "STOPPED"}:
            directories = client.describe_workspace_directories(
                DirectoryIds=[os.environ["DIRECTORY_ID"]]
            )["Directories"]
            if len(directories) != 1 or not directories[0].get("RegistrationCode"):
                raise AuthError(503, "directory_unavailable", "登録コードを取得できません。管理者にお問い合わせください。")
            uri = "workspaces://" + quote(session["login"]["S"], safe="") + "@" + quote(
                directories[0]["RegistrationCode"], safe=""
            )
            return page_response(
                200, "パーソナルデスクトップに接続",
                "<h1>パーソナルデスクトップに接続できます</h1>"
                f'<p><a href="{escape(uri, quote=True)}">WorkSpaces クライアントを起動する</a></p>'
                "<p>起動を許可し、登録時に設定したパスワードを入力してください。</p>"
                f'<p>起動できない場合は <a href="{CLIENT_DOWNLOAD}">'
                "WorkSpaces DCV クライアントをインストール</a>してください。</p>"
                "<p>GitHub のユーザー名は変更しないでください。</p>",
            )
        if state in {"PENDING", "STARTING", "REBOOTING", "REBUILDING", "UPDATING", "MAINTENANCE", "STOPPING"}:
            return windows_waiting_response()
        raise AuthError(503, "workspace_" + state.lower(), "WorkSpace を利用できません。管理者にお問い合わせください。")
    now = int(time.time())
    if "workspace_failure" in record:
        raise AuthError(503, "workspace_creation_rejected", "WorkSpace の作成が拒否されています。管理者にお問い合わせください。")
    if "workspace_requested" in record:
        if now - int(record["workspace_requested"]["N"]) > 1800:
            raise AuthError(503, "workspace_request_uncertain", "作成要求の結果を確認できません。管理者にお問い合わせください。")
        return windows_waiting_response()
    # Persist intent before the API call: CreateWorkspaces has no idempotency token.
    update_user(session, "SET workspace_requested = :now", {":now": {"N": str(now)}})
    request = {
        "DirectoryId": os.environ["DIRECTORY_ID"],
        "UserName": session["login"]["S"],
        "BundleId": os.environ["WORKSPACE_BUNDLE_ID"],
        "WorkspaceProperties": {
            "RunningMode": "AUTO_STOP", "RunningModeAutoStopTimeoutInMinutes": 60,
            "Protocols": ["WSP"],
        },
        "Tags": [{"Key": "GitHubUserId", "Value": session["user_id"]["S"]}],
    }
    try:
        result = client.create_workspaces(Workspaces=[request])
    except ClientError as error:
        update_user(
            session, "SET workspace_failure = :code",
            {":code": {"S": error.response["Error"]["Code"]}},
        )
        raise
    failures = result.get("FailedRequests", [])
    if failures:
        code = failures[0].get("ErrorCode", "unknown")
        LOGGER.error("WorkSpace creation rejected: %s", code)
        if code == "ResourceNotFound.User":
            update_user(
                session, "REMOVE workspace_requested SET ad_ready = :ready",
                {":ready": {"BOOL": True}},
            )
            return windows_waiting_response()
        update_user(session, "SET workspace_failure = :code", {":code": {"S": code}})
        raise AuthError(503, "workspace_creation_rejected", "WorkSpace の作成が拒否されました。管理者にお問い合わせください。")
    if len(result.get("PendingRequests", [])) != 1:
        raise AuthError(503, "workspace_invalid_response", "作成結果を確認できません。管理者にお問い合わせください。")
    LOGGER.info("WorkSpace requested for GitHub user ID %s", session["user_id"]["S"])
    return windows_waiting_response()


def windows_response(event, key, session, form):
    validate_windows_login(session)
    if form is not None:
        aws_client("dynamodb").update_item(
            TableName=os.environ["STATE_TABLE"], Key={"state_id": {"S": key}},
            UpdateExpression="SET windows_started = :yes",
            ConditionExpression="expires_at > :now",
            ExpressionAttributeValues={
                ":yes": {"BOOL": True}, ":now": {"N": str(int(time.time()))},
            },
        )
    elif not session.get("windows_started", {}).get("BOOL"):
        return response(302, location="/desktop")
    record = user_record(session)
    if record is not None and record["login"]["S"] != session["login"]["S"]:
        raise AuthError(403, "github_login_changed", "登録後の GitHub ユーザー名変更は禁止されています。管理者にお問い合わせください。")
    if (
        form is not None and "password" in form
        and (record is None or not record.get("ad_ready", {}).get("BOOL"))
    ):
        message = validate_password(form, session["login"]["S"])
        if message:
            return password_response(session, message, 400)
    if record is None:
        if form is None or "password" not in form:
            return password_response(session)
        record = claim_user(session)
    db = aws_client("dynamodb")
    lease = secrets.token_urlsafe(16)
    try:
        db.update_item(
            TableName=os.environ["USERS_TABLE"],
            Key={"principal": {"S": "github:" + session["user_id"]["S"]}},
            UpdateExpression="SET lease = :lease, lease_until = :until",
            ConditionExpression="attribute_not_exists(lease_until) OR lease_until < :now",
            ExpressionAttributeValues={
                ":lease": {"S": lease}, ":until": {"N": str(int(time.time()) + 60)},
                ":now": {"N": str(int(time.time()))},
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return windows_waiting_response()
        raise
    try:
        # Refresh after locking; another request may have completed registration.
        record = user_record(session)
        if not record.get("ad_ready", {}).get("BOOL"):
            if form is None or "password" not in form:
                return password_response(session)
            result = register_directory_user(session, form)
            if result is not None:
                return result
        else:
            user = directory_user(session)
            if user is None or not user.get("Enabled"):
                raise AuthError(403, "directory_user_unavailable", "登録済みユーザーが削除または無効化されています。管理者にお問い合わせください。")
        return workspace_response(session, record)
    finally:
        db.update_item(
            TableName=os.environ["USERS_TABLE"],
            Key={"principal": {"S": "github:" + session["user_id"]["S"]}},
            UpdateExpression="REMOVE lease, lease_until",
            ConditionExpression="lease = :lease",
            ExpressionAttributeValues={":lease": {"S": lease}},
        )


def lambda_handler(event, context):
    try:
        route = event.get("routeKey")
        if route in STATIC_ASSETS:
            filename, content_type = STATIC_ASSETS[route]
            result = response(
                200, base64.b64encode(Path(__file__).with_name(filename).read_bytes()).decode("ascii")
            )
            result["headers"]["Content-Type"] = content_type
            result["isBase64Encoded"] = True
            return result
        if route in ("GET /", "GET /login"):
            return start_login()
        if route == "GET /callback":
            return finish_login(event)
        if route == "GET /session-ended":
            return session_ended_response()
        if route in {"GET /desktop", "POST /linux", "GET /linux", "POST /windows", "GET /windows"}:
            key, session = authenticated_session(event)
            form = posted_form(event, session) if route.startswith("POST ") else None
            if route == "GET /desktop":
                return desktop_response(session)
            if route == "POST /linux":
                aws_client("dynamodb").update_item(
                    TableName=os.environ["STATE_TABLE"], Key={"state_id": {"S": key}},
                    UpdateExpression="SET linux_started = :yes",
                    ConditionExpression="expires_at > :now",
                    ExpressionAttributeValues={
                        ":yes": {"BOOL": True}, ":now": {"N": str(int(time.time()))},
                    },
                )
                # End the form submission before starting AppStream's cross-origin redirect chain.
                return page_response(
                    200, "セキュアブラウザに接続",
                    "<h1>セキュアブラウザに接続しています</h1>"
                    '<p>自動で進まない場合は <a href="/linux">接続を続ける</a>を押してください。</p>',
                    head='<meta http-equiv="refresh" content="0;url=/linux">',
                )
            if route == "GET /linux":
                if not session.get("linux_started", {}).get("BOOL"):
                    return response(302, location="/desktop")
                return launch_linux(session)
            return windows_response(event, key, session, form)
        return page_response(404, "ページが見つかりません", '<h1>ページが見つかりません</h1><p><a href="/login">ログイン</a></p>')
    except AuthError as error:
        LOGGER.warning("Authentication rejected: %s", error.code)
        return page_response(
            error.status, "処理を完了できませんでした",
            f"<h1>処理を完了できませんでした</h1><p>{escape(error.message)}</p>"
            '<p><a href="/login">ログインし直す</a></p>',
        )
    except (ClientError, BotoCoreError) as error:
        code = (
            error.response["Error"]["Code"]
            if isinstance(error, ClientError)
            else type(error).__name__
        )
        LOGGER.error("AWS authentication dependency failed: %s", code)
        return page_response(
            503, "サービスを利用できません",
            "<h1>サービスを利用できません</h1><p>管理者にお問い合わせいただくか、"
            '時間をおいて再試行してください。</p><p><a href="/login">ログインし直す</a></p>',
        )
