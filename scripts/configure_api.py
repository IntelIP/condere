"""Prepare local API identity in Doppler; creates no campaign or provider credential."""

import json
import secrets
from pathlib import Path
from uuid import uuid4

from doppler_config import doppler

from condere.interfaces.server import verified_image

ORIGIN = "http://127.0.0.1:7778"


def main() -> None:
    values = json.loads(doppler("secrets", "download", "--no-file", "--format", "json"))
    if values.get("CONDERE_API_URL", ORIGIN) != ORIGIN:
        raise SystemExit("Existing API selects another origin; local setup will not replace it.")
    defaults = {
        "CONDERE_API_URL": ORIGIN,
        "CONDERE_API_TOKEN": secrets.token_urlsafe(32),
        "CONDERE_OWNER_ID": "condere-local-" + str(uuid4()),
        "CONDERE_EXPORT_DIRECTORY": str(Path.home() / ".local/share/condere-mda/exports"),
        "CONDERE_ARTIFACT_DIRECTORY": str(Path.home() / ".local/share/condere-mda/artifacts"),
    }
    manifest = Path(__file__).resolve().parents[1] / "infra/smolvm/inspection-image.json"
    if manifest.is_file():
        defaults["CONDERE_SMOL_IMAGE"] = verified_image(json.loads(manifest.read_text())["archive"])
    for name, value in defaults.items():
        if not values.get(name):
            values[name] = value
    doppler("secrets", "upload", "/dev/stdin", payload=json.dumps(values))
    print("Prepared local API identity in condere/dev; no services started.")


if __name__ == "__main__":
    main()
