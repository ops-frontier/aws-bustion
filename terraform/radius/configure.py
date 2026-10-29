import json
import os
import grp
from pathlib import Path

import boto3
from botocore.config import Config


def main():
    config = json.loads(Path("/etc/bustion-radius.json").read_text())
    client = boto3.client(
        "secretsmanager",
        config=Config(region_name=config["region"], retries={"max_attempts": 10}),
    )
    secret = client.get_secret_value(SecretId=config["secret"])["SecretString"]
    if len(secret) != 64 or not secret.isascii() or not secret.isalnum():
        raise ValueError("Invalid RADIUS shared secret format")
    clients = []
    for index, address in enumerate(config["clients"]):
        clients.append(
            f"client directory{index} {{\n"
            f" ipaddr = {address}\n proto = udp\n secret = {secret}\n"
            " require_message_authenticator = yes\n"
            "}\n"
        )
    path = Path("/etc/freeradius/3.0/clients.conf")
    os.chmod(path, 0o600)
    path.write_text("".join(clients))
    os.chown(path, 0, grp.getgrnam("freerad").gr_gid)
    os.chmod(path, 0o640)


if __name__ == "__main__":
    main()
