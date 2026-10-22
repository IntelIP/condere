"""Build and save a hash-identified inspection image for the local SmolVM engine."""

import hashlib
import json
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    base = json.loads((root / "infra/smolvm/base-image.json").read_text())["image"]
    if "@sha256:" not in base:
        raise SystemExit("The inspection image requires a pinned base image.")
    tag = "condere-mda-inspection:local"
    subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            f"BASE_IMAGE={base}",
            "--tag",
            tag,
            str(root / "infra/smolvm"),
        ],
        check=True,
    )
    artifact = root / ".runtime/images/condere-inspection.tar"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact.with_suffix(".tar.partial")
    subprocess.run(["docker", "save", "--output", str(temporary), tag], check=True)
    with temporary.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    temporary.replace(artifact)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
    ).strip()
    record = {
        "base_image": base,
        "image_id": image_id,
        "archive": str(artifact.relative_to(root)),
        "archive_sha256": digest,
        "archive_bytes": artifact.stat().st_size,
    }
    (root / "infra/smolvm/inspection-image.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
