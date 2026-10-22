"""Run only the pinned, task-owned local evidence service; Ctrl-C retains its data."""

import hashlib
import json
import os
from pathlib import Path

ROOT = Path.home() / ".local/share/condere-mda/evidence-store"
BINARY = Path.home() / ".local/share/condere-mda/minio/bin/minio"
ENDPOINT = "http://127.0.0.1:19000"


def main() -> None:
    required = (
        "CONDERE_EVIDENCE_ENDPOINT",
        "CONDERE_EVIDENCE_ACCESS_KEY",
        "CONDERE_EVIDENCE_SECRET_KEY",
        "CONDERE_EVIDENCE_REGION",
        "CONDERE_EVIDENCE_BUCKET",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing evidence bindings: " + ", ".join(missing))
    if (
        os.environ["CONDERE_EVIDENCE_ENDPOINT"] != ENDPOINT
        or os.environ["CONDERE_EVIDENCE_BUCKET"] != "condere-evidence"
        or os.environ["CONDERE_EVIDENCE_REGION"] != "us-east-1"
    ):
        raise SystemExit(
            "This launcher supports only the task's dedicated loopback evidence store."
        )
    pin = json.loads(
        (Path(__file__).resolve().parents[1] / "infra/evidence/minio.json").read_text()
    )
    if not BINARY.is_file():
        raise SystemExit("Build the pinned MinIO binary using the infrastructure guide first.")
    with BINARY.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["binary_sha256"]:
            raise SystemExit("The local MinIO binary differs from the verified native build.")
    marker = ROOT / "owner.json"
    ownership = {"application": "condere-mda", "purpose": "evidence", "endpoint": ENDPOINT}
    if ROOT.is_symlink() or (ROOT.exists() and not marker.exists() and any(ROOT.iterdir())):
        raise SystemExit("An existing unowned evidence directory will not be adopted or replaced.")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    if marker.exists():
        if json.loads(marker.read_text()) != ownership:
            raise SystemExit("The evidence directory ownership does not match this task.")
    else:
        marker.write_text(json.dumps(ownership) + "\n")
    for name in ("data", "certs"):
        (ROOT / name).mkdir(exist_ok=True, mode=0o700)
    environment = {
        name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR") if name in os.environ
    }
    environment.update(
        {
            "MINIO_ROOT_USER": os.environ["CONDERE_EVIDENCE_ACCESS_KEY"],
            "MINIO_ROOT_PASSWORD": os.environ["CONDERE_EVIDENCE_SECRET_KEY"],
            "MINIO_REGION_NAME": os.environ["CONDERE_EVIDENCE_REGION"],
            "MINIO_BROWSER": "off",
            "MINIO_UPDATE": "off",
            "MINIO_CALLHOME_ENABLE": "off",
        }
    )
    os.execve(
        str(BINARY),
        [
            str(BINARY),
            "server",
            "--address",
            "127.0.0.1:19000",
            "--console-address",
            "127.0.0.1:19001",
            "--certs-dir",
            str(ROOT / "certs"),
            "--quiet",
            "--anonymous",
            str(ROOT / "data"),
        ],
        environment,
    )


if __name__ == "__main__":
    main()
