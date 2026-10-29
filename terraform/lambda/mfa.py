import base64
import hashlib
import hmac
import re
import secrets
import struct
import time

from botocore.exceptions import ClientError


ENROLLMENT_SECONDS = 600
ATTEMPT_SECONDS = 300
MAX_ATTEMPTS = 5
LOGIN_PATTERN = r"[a-z0-9](?:[a-z0-9_-]{0,18}[a-z0-9])?"


class MFAError(Exception):
    pass


def totp(secret, step):
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1000000:06d}"


class MFAStore:
    def __init__(self, db, kms, table, key_id):
        self.db = db
        self.kms = kms
        self.table = table
        self.key_id = key_id

    def get(self, login):
        return self.db.get_item(
            TableName=self.table, Key={"login": {"S": login}}, ConsistentRead=True,
        ).get("Item")

    def context(self, login):
        return {"purpose": "bustion-totp", "login": login}

    def secret(self, record):
        return self.kms.decrypt(
            KeyId=self.key_id,
            CiphertextBlob=base64.b64decode(record["ciphertext"]["S"], validate=True),
            EncryptionContext=self.context(record["login"]["S"]),
        )["Plaintext"]

    def prepare(self, login, user_id):
        now = int(time.time())
        record = self.get(login)
        if record is not None:
            if record["user_id"]["S"] != user_id:
                raise MFAError("identity_conflict")
            if record["active"]["BOOL"] or int(record["pending_until"]["N"]) > now:
                return record
        ciphertext = self.kms.encrypt(
            KeyId=self.key_id, Plaintext=secrets.token_bytes(20),
            EncryptionContext=self.context(login),
        )["CiphertextBlob"]
        item = {
            "login": {"S": login}, "user_id": {"S": user_id},
            "ciphertext": {"S": base64.b64encode(ciphertext).decode("ascii")},
            "generation": {"S": secrets.token_hex(16)},
            "active": {"BOOL": False}, "pending_until": {"N": str(now + ENROLLMENT_SECONDS)},
            "last_step": {"N": "-1"}, "attempts": {"N": "0"},
            "window_until": {"N": str(now + ATTEMPT_SECONDS)},
        }
        try:
            self.db.put_item(
                TableName=self.table, Item=item,
                ConditionExpression=(
                    "attribute_not_exists(login) OR "
                    "(active = :no AND pending_until <= :now AND user_id = :user)"
                ),
                ExpressionAttributeValues={
                    ":no": {"BOOL": False}, ":now": {"N": str(now)}, ":user": {"S": user_id},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise MFAError("enrollment_changed") from error
        return item

    def verify(self, login, code, *, enrolling=False, user_id=None):
        now = int(time.time())
        if not re.fullmatch(LOGIN_PATTERN, login):
            raise MFAError("invalid_login")
        record = self.get(login)
        if record is None or record["active"]["BOOL"] == enrolling:
            raise MFAError("not_enrolled")
        if user_id is not None and record["user_id"]["S"] != user_id:
            raise MFAError("identity_conflict")
        if enrolling and int(record["pending_until"]["N"]) <= now:
            raise MFAError("enrollment_expired")
        generation = record["generation"]
        key = {"login": {"S": login}}
        try:
            self.db.update_item(
                TableName=self.table, Key=key,
                UpdateExpression="SET attempts = :zero, window_until = :until",
                ConditionExpression="generation = :generation AND window_until <= :now",
                ExpressionAttributeValues={
                    ":zero": {"N": "0"}, ":until": {"N": str(now + ATTEMPT_SECONDS)},
                    ":generation": generation, ":now": {"N": str(now)},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        try:
            self.db.update_item(
                TableName=self.table, Key=key,
                UpdateExpression="ADD attempts :one",
                ConditionExpression="generation = :generation AND attempts < :limit",
                ExpressionAttributeValues={
                    ":one": {"N": "1"}, ":limit": {"N": str(MAX_ATTEMPTS)},
                    ":generation": generation,
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise MFAError("rate_limited") from error
        step = None
        if re.fullmatch(r"[0-9]{6}", code):
            secret = self.secret(record)
            for candidate in (now // 30, now // 30 - 1, now // 30 + 1):
                if hmac.compare_digest(totp(secret, candidate), code):
                    step = candidate
                    break
        if step is None:
            raise MFAError("invalid_code")
        values = {
            ":step": {"N": str(step)}, ":zero": {"N": "0"},
            ":generation": generation, ":active": {"BOOL": not enrolling},
        }
        condition = "generation = :generation AND active = :active AND last_step < :step"
        expression = "SET last_step = :step, attempts = :zero"
        if enrolling:
            condition += " AND pending_until > :now"
            values.update({":now": {"N": str(now)}, ":yes": {"BOOL": True}})
            expression += ", active = :yes"
        try:
            self.db.update_item(
                TableName=self.table, Key=key, UpdateExpression=expression,
                ConditionExpression=condition, ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise MFAError("replayed_or_changed") from error
