"""Deep Agents sandbox on Modal: the cloud backend for untrusted repository code.

The runtime downloads and verifies each GitHub archive itself, then uploads it, so the sandbox
never needs a network: every sandbox starts with `block_network=True` in a US region. The image
is the SmolVM inspection image, built by Modal from the same Dockerfile and locked requirements.
Modal has no stopped state, so closing always terminates; command artifacts stay on the host.
"""

import base64
import json
import threading
import uuid
from pathlib import Path

import modal
from deepagents.backends.protocol import FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox

from condere.runtime.sandbox import _READ_FILE, MACHINE_NAME, SandboxResult, transfer_path

ROOT = Path(__file__).resolve().parents[3]
APP_NAME = "condere-sandboxes"
# Repository exploration stays in the US (AGENTS.md).
REGION = "us-east"
LIFETIME_SECONDS = 2 * 3600
TAGS = {"application": "condere", "network": "offline"}
_images: dict[str, modal.Image] = {}
_images_lock = threading.Lock()


def analysis_image(base_image: str) -> modal.Image:
    with _images_lock:
        if base_image not in _images:
            _images[base_image] = modal.Image.from_dockerfile(
                ROOT / "infra/smolvm/Dockerfile",
                context_dir=ROOT,
                build_args={"BASE_IMAGE": base_image},
            )
        return _images[base_image]


def _app() -> modal.App:
    return modal.App.lookup(APP_NAME, create_if_missing=True)


class ModalSandbox(BaseSandbox):
    def __init__(
        self,
        sandbox: modal.Sandbox,
        name: str,
        artifact_dir: Path,
        *,
        output_limit: int = 2 * 1024 * 1024,
        file_limit: int = 4 * 1024 * 1024,
    ) -> None:
        self.sandbox, self.name = sandbox, name
        self.artifact_dir = artifact_dir.resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.output_limit = output_limit
        self.file_limit = file_limit
        self._command_lock = threading.Lock()

    @classmethod
    def create(cls, name: str, image: str, artifact_dir: Path, **kwargs) -> "ModalSandbox":
        """`image` is the inspection image's base image reference (inspection-image.json)."""
        if not MACHINE_NAME.fullmatch(name):
            raise ValueError("Machine name must be a task-owned Condere identifier.")
        sandbox = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=_app(),
            name=name,
            image=analysis_image(image),
            block_network=True,
            region=REGION,
            cpu=2,
            memory=2048,
            timeout=LIFETIME_SECONDS,
            workdir="/workspace",
            tags=TAGS | {"machine": name},
        )
        created = cls(sandbox, name, artifact_dir, **kwargs)
        prepared = created.execute("mkdir -p /workspace", timeout=30)
        if prepared.exit_code != 0:
            sandbox.terminate()
            raise RuntimeError("Sandbox workspace initialization failed.")
        return created

    @classmethod
    def reconnect(cls, name: str, artifact_dir: Path, **kwargs) -> "ModalSandbox":
        if not MACHINE_NAME.fullmatch(name):
            raise ValueError("Invalid Condere machine identifier.")
        return cls(modal.Sandbox.from_name(APP_NAME, name), name, artifact_dir, **kwargs)

    @staticmethod
    def running(prefix: str) -> list[str]:
        names = []
        for sandbox in modal.Sandbox.list(app_id=_app().app_id, tags=TAGS):
            name = sandbox.get_tags().get("machine", "")
            if name.startswith(prefix) and sandbox.poll() is None:
                names.append(name)
        return names

    @property
    def id(self) -> str:
        return self.name

    def execute(self, command: str, *, timeout: int | None = None) -> SandboxResult:
        timeout = 120 if timeout is None else timeout
        if not 1 <= timeout <= 300:
            raise ValueError("Sandbox command timeout must be between 1 and 300 seconds.")
        artifact = self.artifact_dir / f"{uuid.uuid4().hex}.txt"
        data = bytearray()
        exit_code = None
        truncated = False
        with self._command_lock:
            try:
                process = self.sandbox.exec(
                    "/bin/sh",
                    "-c",
                    # One stream keeps stdout and stderr in order, as SmolVM's events do.
                    "exec 2>&1\n" + command,
                    timeout=timeout,
                    workdir="/workspace",
                    text=False,
                )
                for chunk in process.stdout:
                    chunk = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                    remaining = self.output_limit - len(data)
                    data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        # The process ends with its exec timeout; the output is already bounded.
                        truncated, exit_code = True, 125
                        break
                if not truncated:
                    exit_code = process.wait()
            except Exception as exc:
                data.extend(f"\nSandbox execution failed ({type(exc).__name__}).\n".encode())
                exit_code = 1
            finally:
                artifact.write_bytes(bytes(data[: self.output_limit]))
        return SandboxResult(
            output=data.decode(errors="replace"),
            exit_code=exit_code,
            truncated=truncated,
            artifact_path=str(artifact),
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for path, content in files:
            try:
                transfer_path(path)
                if len(content) > self.file_limit:
                    results.append(FileUploadResponse(path=path, error="file_too_large"))
                    continue
                self.sandbox.filesystem.write_bytes(content, path)
                results.append(FileUploadResponse(path=path))
            except ValueError:
                results.append(FileUploadResponse(path=path, error="invalid_path"))
            except Exception:
                results.append(FileUploadResponse(path=path, error="sandbox_file_error"))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for path in paths:
            try:
                transfer_path(path)
                # Resolve inside the guest, as SmolVM does: a symlink must not escape /workspace.
                process = self.sandbox.exec(
                    "python", "-c", _READ_FILE, path, str(self.file_limit), timeout=30
                )
                output = process.stdout.read()
                if process.wait() != 0:
                    raise OSError("Guest download failed.")
                payload = json.loads(output)
                results.append(
                    FileDownloadResponse(
                        path=path,
                        content=base64.b64decode(payload["content"], validate=True)
                        if "content" in payload
                        else None,
                        error=payload.get("error"),
                    )
                )
            except ValueError:
                results.append(FileDownloadResponse(path=path, error="invalid_path"))
            except Exception:
                results.append(FileDownloadResponse(path=path, error="sandbox_file_error"))
        return results

    def write_trusted(self, path: str, content: bytes) -> None:
        self.sandbox.filesystem.write_bytes(content, str(transfer_path(path)))

    def read_trusted(self, path: str) -> bytes:
        return self.sandbox.filesystem.read_bytes(str(transfer_path(path)))

    def cancel(self) -> None:
        """A running command ends with its timeout or when `close` terminates the sandbox."""

    def close(self, *, delete: bool = False) -> None:
        self.sandbox.terminate()
