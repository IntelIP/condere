"""Prepare credentials for the task's isolated local evidence server and bucket."""

import argparse
import secrets

from botocore.exceptions import BotoCoreError, ClientError
from local_config import arguments, load, save

from condere.runtime.evidence import object_client

ENDPOINT = "http://127.0.0.1:19000"
BUCKET = "condere-evidence"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize-bucket", action="store_true")
    args = arguments(parser)
    original = load(args)
    values = dict(original)
    defaults = {
        "CONDERE_EVIDENCE_ENDPOINT": ENDPOINT,
        "CONDERE_EVIDENCE_REGION": "us-east-1",
        "CONDERE_EVIDENCE_BUCKET": BUCKET,
        "CONDERE_EVIDENCE_ACCESS_KEY": "condere" + secrets.token_hex(12),
        "CONDERE_EVIDENCE_SECRET_KEY": secrets.token_urlsafe(40),
    }
    for name, expected in (
        ("CONDERE_EVIDENCE_ENDPOINT", ENDPOINT),
        ("CONDERE_EVIDENCE_BUCKET", BUCKET),
        ("CONDERE_EVIDENCE_REGION", "us-east-1"),
    ):
        if values.get(name, expected) != expected:
            raise SystemExit("Existing evidence configuration selects another store; not replaced.")
    if not args.initialize_bucket:
        for name, value in defaults.items():
            if not values.get(name):
                values[name] = value
        save(args, original, values)
        print("Prepared dedicated local evidence bindings; no services started.")
        return
    if any(not values.get(name) for name in defaults):
        raise SystemExit("Prepare evidence credentials before initializing its bucket.")
    client = object_client(values)
    try:
        try:
            client.head_bucket(Bucket=BUCKET)
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code")) not in {"404", "NoSuchBucket"}:
                raise
            client.create_bucket(Bucket=BUCKET)
        client.head_bucket(Bucket=BUCKET)
    except (BotoCoreError, ClientError):
        raise SystemExit(
            "Local evidence bucket setup failed; credentials were not displayed."
        ) from None
    finally:
        client.close()
    print("Dedicated evidence bucket is reachable; no existing objects were changed.")


if __name__ == "__main__":
    main()
