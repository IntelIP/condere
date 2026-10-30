"""Build an inspection archive inside a native VM, without Docker or host mounts."""

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from uuid import uuid4

from smol import ConnectOptions, ExecOptions, Machine, MachineConfig, ResourceSpec

from condere.runtime.sandbox import SmolSandbox

EXPORT = """
import os, pathlib, tarfile
root_device = os.stat('/').st_dev
with tarfile.open('/tmp/condere-layer.tar', 'w') as archive:
    def add(path):
        archive.add(path, arcname=str(path).lstrip('/'), recursive=False)
        # Keep mount-point directories, but never export guest engine storage,
        # procfs/devices, temporary files, or the separately mounted workspace.
        if path.is_dir() and not path.is_symlink() and path.stat().st_dev == root_device:
            for child in sorted(path.iterdir()):
                add(child)
    for path in sorted(pathlib.Path('/').iterdir()):
        if path.name != 'storage':
            add(path)
"""


def execute(machine, command, timeout=300):
    result = machine.exec(command, ExecOptions(timeout=timeout, user="root"))
    if result.exit_code:
        raise RuntimeError(f"Inspection build command failed: {result.stderr[-2000:]}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="Resume a retained task-owned image builder.")
    args = parser.parse_args()
    if args.resume and not re.fullmatch(r"condere-image-build-[0-9a-f]{12}", args.resume):
        raise ValueError("Only a task-owned inspection image builder may be resumed.")
    root = Path(__file__).resolve().parents[1]
    base = json.loads((root / "infra/smolvm/analysis-base-image.json").read_text())["image"]
    if "@sha256:" not in base:
        raise SystemExit("The inspection image requires a pinned base image.")
    name = args.resume or "condere-image-build-" + uuid4().hex[:12]
    # Only this build VM has network access. No case data, credentials, host mounts,
    # or forwarded ports enter it. Assessment VMs remain offline.
    machine = (
        Machine.connect(name, ConnectOptions(target="local"))
        if args.resume
        else Machine.create(
            MachineConfig(
                name=name,
                image=base,
                command=["sleep", "infinity"],
                network=True,
                resources=ResourceSpec(cpus=2, memory_mb=2048),
                mounts=[],
                ports=[],
                env={},
                credentials=[],
                persistent=True,
                labels={"application": "condere", "purpose": "inspection-image-build"},
            ),
            ConnectOptions(target="local"),
        )
    )
    if args.resume:
        machine.start()
    success = False
    directory = root / ".runtime/images"
    directory.mkdir(parents=True, exist_ok=True)
    layer_path = directory / (name + "-layer.tar")
    try:
        machine.write_file(
            "/tmp/condere-requirements.txt",
            (root / "infra/smolvm/requirements.txt").read_bytes(),
        )
        machine.write_file(
            "/tmp/condere-graphify.txt", (root / "infra/smolvm/graphify.txt").read_bytes()
        )
        execute(
            machine,
            [
                "sh",
                "-c",
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "--no-install-recommends bash git ripgrep build-essential sqlite3 libsqlite3-dev "
                "fonts-dejavu-core ca-certificates && "
                "uv venv --allow-existing /opt/condere-tools --python /usr/local/bin/python && "
                "uv pip sync --python /opt/condere-tools/bin/python --require-hashes "
                "/tmp/condere-requirements.txt && "
                "uv pip install --python /opt/condere-tools/bin/python --no-deps "
                "--require-hashes -r /tmp/condere-graphify.txt && "
                "(id -u researcher >/dev/null 2>&1 || "
                "useradd -m -d /workspace -s /bin/bash researcher) && "
                "mkdir -p /workspace && "
                "chown researcher:researcher /workspace && "
                "rm -rf /var/lib/apt/lists/* /root/.cache/uv",
            ],
        )
        packages = execute(machine, ["dpkg-query", "-W", "-f=${Package} ${Version}\n"]).splitlines()
        execute(machine, ["python", "-c", EXPORT])
        guest_hash = execute(machine, ["sha256sum", "/tmp/condere-layer.tar"]).split()[0]
        layer_path.write_bytes(machine.read_file("/tmp/condere-layer.tar"))
        with layer_path.open("rb") as handle:
            layer_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        if layer_hash != guest_hash:
            raise RuntimeError("The exported build filesystem changed during transfer.")
        architecture = execute(machine, ["uname", "-m"]).strip()
        architecture = {"aarch64": "arm64", "x86_64": "amd64"}[architecture]
        config = json.dumps(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {
                    "User": "researcher",
                    "WorkingDir": "/workspace",
                    "Cmd": ["sleep", "infinity"],
                    "Env": [
                        "PATH=/opt/condere-tools/bin:/usr/local/bin:/usr/local/sbin:"
                        "/usr/sbin:/usr/bin:/sbin:/bin"
                    ],
                    "Labels": {"org.opencontainers.image.base.name": base},
                },
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + layer_hash]},
            },
            sort_keys=True,
        ).encode()
        image_id = hashlib.sha256(config).hexdigest()
        config_name = image_id + ".json"
        manifest = json.dumps(
            [
                {
                    "Config": config_name,
                    "RepoTags": ["condere-inspection:local"],
                    "Layers": ["layer.tar"],
                }
            ]
        ).encode()
        artifact = directory / ("condere-inspection-" + image_id + ".tar")
        temporary = artifact.with_suffix(".tar.partial")
        with tarfile.open(temporary, "w") as archive:
            for filename, content in ((config_name, config), ("manifest.json", manifest)):
                entry = tarfile.TarInfo(filename)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            entry = tarfile.TarInfo("layer.tar")
            entry.size = layer_path.stat().st_size
            with layer_path.open("rb") as handle:
                archive.addfile(entry, handle)
        with temporary.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        temporary.replace(artifact)
        sandbox = SmolSandbox.create(
            "condere-image-check-" + uuid4().hex[:12],
            str(artifact),
            root / ".artifacts/infrastructure/image-check",
        )
        try:
            result = sandbox.execute(
                "test $(id -u) -ne 0 && test -w /workspace && "
                "python --version && git --version && rg --version && "
                "cc --version && sqlite3 --version && "
                "python -c 'import graphify, matplotlib; print(matplotlib.__version__)'"
            )
            if result.exit_code:
                raise RuntimeError("The exported image failed its non-root tooling check.")
        finally:
            sandbox.close(delete=True)
        record = {
            "base_image": base,
            "image_id": "sha256:" + image_id,
            "archive": str(artifact.relative_to(root)),
            "archive_sha256": digest,
            "archive_bytes": artifact.stat().st_size,
            "architecture": architecture,
            "build_mode": "native SmolVM; no Docker",
            "packages": packages,
            "python_requirements": {
                name: hashlib.sha256((root / "infra/smolvm" / name).read_bytes()).hexdigest()
                for name in ("requirements.txt", "graphify.txt")
            },
        }
        (root / "infra/smolvm/inspection-image.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        print(json.dumps({key: value for key, value in record.items() if key != "packages"}))
        success = True
    finally:
        layer_path.unlink(missing_ok=True)
        if success:
            machine.delete()
        else:
            machine.stop()
            print(f"Build did not finish; retained stopped builder {name} for diagnosis.")


if __name__ == "__main__":
    main()
