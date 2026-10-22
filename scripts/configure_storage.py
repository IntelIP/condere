"""Create local PostgreSQL credentials without changing the existing hosted DB_URL."""

import json
import secrets
from urllib.parse import quote, urlsplit

from configure_langfuse import doppler


def main() -> None:
    values = json.loads(doppler("secrets", "download", "--no-file", "--format", "json"))
    for key in ("CONDERE_DATABASE_URL", "CONDERE_CHECKPOINT_URL"):
        if values.get(key):
            parsed = urlsplit(values[key])
            if parsed.hostname != "127.0.0.1" or parsed.port != 15434:
                raise SystemExit(f"{key} selects another host; local setup will not replace it.")
    for key in (
        "CONDERE_PG_ADMIN_PASSWORD",
        "CONDERE_DATABASE_PASSWORD",
        "CONDERE_CHECKPOINT_PASSWORD",
    ):
        if not values.get(key):
            values[key] = secrets.token_urlsafe(32)
    for key, user, database, password_key in (
        ("CONDERE_DATABASE_URL", "condere_app", "condere_mda", "CONDERE_DATABASE_PASSWORD"),
        (
            "CONDERE_CHECKPOINT_URL",
            "condere_checkpoint",
            "condere_mda_checkpoints",
            "CONDERE_CHECKPOINT_PASSWORD",
        ),
    ):
        password = quote(values[password_key], safe="")
        values[key] = f"postgresql://{user}:{password}@127.0.0.1:15434/{database}?sslmode=disable"
    doppler("secrets", "upload", "/dev/stdin", payload=json.dumps(values))
    print(
        "Prepared separate local application/checkpoint credentials in condere/dev."
    )


if __name__ == "__main__":
    main()
