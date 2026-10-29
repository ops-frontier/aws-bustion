import json
import logging
import re
from pathlib import Path

import boto3
import radiusd
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from mfa import MFAError, MFAStore


STORE = None


def instantiate(_):
    global STORE
    config = json.loads(Path("/etc/bustion-radius.json").read_text())
    settings = Config(
        region_name=config["region"], connect_timeout=2, read_timeout=2,
        retries={"total_max_attempts": 1},
    )
    STORE = MFAStore(
        boto3.client("dynamodb", config=settings), boto3.client("kms", config=settings),
        config["table"], config["key"],
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    return radiusd.RLM_MODULE_OK


def authenticate(attributes):
    values = {}
    for name, value in attributes:
        if name in {"User-Name", "User-Password"}:
            if name in values:
                radiusd.radlog(radiusd.L_ERR, "OTP rejected: duplicate attribute")
                return radiusd.RLM_MODULE_REJECT
            values[name] = value
    login = values.get("User-Name", "").lower()
    code = values.get("User-Password", "")
    if not login or not re.fullmatch(r"[0-9]{6}", code):
        radiusd.radlog(radiusd.L_ERR, "OTP rejected: malformed request")
        return radiusd.RLM_MODULE_REJECT
    if STORE is None:
        radiusd.radlog(radiusd.L_ERR, "OTP rejected: verifier not initialized")
        return radiusd.RLM_MODULE_FAIL
    try:
        STORE.verify(login, code)
    except MFAError as error:
        radiusd.radlog(radiusd.L_ERR, "OTP rejected: " + str(error))
        return radiusd.RLM_MODULE_REJECT
    except (BotoCoreError, ClientError) as error:
        code = (
            error.response["Error"]["Code"]
            if isinstance(error, ClientError) else type(error).__name__
        )
        radiusd.radlog(radiusd.L_ERR, "OTP rejected: AWS dependency " + code)
        return radiusd.RLM_MODULE_FAIL
    radiusd.radlog(radiusd.L_INFO, "OTP accepted")
    return radiusd.RLM_MODULE_OK
