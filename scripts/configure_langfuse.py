"""Configure only Condere's dedicated local Langfuse installation in Doppler."""

import json
import secrets
import subprocess
import uuid
from pathlib import Path

PROJECT = "condere"
CONFIG = "dev"
BASE_URL = "http://127.0.0.1:3000"


def doppler(*args: str, payload: str | None = None) -> str:
    result = subprocess.run(
        ["doppler", *args, "--project", PROJECT, "--config", CONFIG],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"Doppler {args[0]} failed; no secret output was displayed.")
    return result.stdout


def main() -> None:
    values = json.loads(doppler("secrets", "download", "--no-file", "--format", "json"))
    initial = values.get("LANGFUSE_BASE_URL") != BASE_URL
    if initial and values.get("LANGFUSE_POSTGRES_PASSWORD"):
        raise SystemExit(
            "Existing installation has another URL; inspect it before changing secrets."
        )
    generated = {
        "LANGFUSE_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_CLICKHOUSE_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_REDIS_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_MINIO_ACCESS_KEY": "condere" + secrets.token_hex(8),
        "LANGFUSE_MINIO_SECRET_KEY": secrets.token_urlsafe(32),
        "LANGFUSE_SALT": secrets.token_hex(32),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_AUTH_SECRET": secrets.token_urlsafe(32),
        "LANGFUSE_ADMIN_EMAIL": "operator@condere.local",
        "LANGFUSE_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + str(uuid.uuid4()),
        "LANGFUSE_SECRET_KEY": "sk-lf-" + str(uuid.uuid4()),
        "LANGFUSE_BASE_URL": BASE_URL,
    }
    for key, value in generated.items():
        if initial or not values.get(key):
            values[key] = value
    root = Path(__file__).resolve().parents[1]
    pins = json.loads((root / "infra/langfuse/images.json").read_text())
    for key, entry in pins.items():
        if "@sha256:" not in entry["image"]:
            raise SystemExit(f"{key} must use an immutable digest.")
        values[key] = entry["image"]
    # Upload receives secrets over stdin; no values appear in argv or a local file.
    doppler("secrets", "upload", "/dev/stdin", payload=json.dumps(values))
    print(f"Configured {PROJECT}/{CONFIG}; credentials remain in Doppler.")


if __name__ == "__main__":
    main()
