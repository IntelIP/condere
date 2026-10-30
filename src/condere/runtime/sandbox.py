"""Deep Agents sandbox backed only by SmolVM's local engine."""

import base64
import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from smol import ConnectOptions, ExecOptions, Machine, MachineConfig, ResourceSpec

_READ_FILE = """
import base64, json, os, pathlib, stat, sys
try:
    path = pathlib.Path(sys.argv[1]).resolve(strict=True)
    if not any(path.is_relative_to(root) for root in ('/workspace', '/tmp')):
        raise PermissionError()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise IsADirectoryError()
        data = handle.read(int(sys.argv[2]) + 1)
    result = {'error': 'file_too_large'} if len(data) > int(sys.argv[2]) else {
        'content': base64.b64encode(data).decode()}
except FileNotFoundError:
    result = {'error': 'file_not_found'}
except PermissionError:
    result = {'error': 'permission_denied'}
except IsADirectoryError:
    result = {'error': 'is_directory'}
print(json.dumps(result))
"""


@dataclass
class SandboxResult(ExecuteResponse):
    """Keep the artifact separate so inherited filesystem tools can parse stdout."""

    artifact_path: str | None = None


class SmolSandbox(BaseSandbox):
    def __init__(
        self,
        machine: Machine,
        artifact_dir: Path,
        *,
        output_limit: int = 2 * 1024 * 1024,
        file_limit: int = 4 * 1024 * 1024,
    ) -> None:
        self.machine = machine
        self.artifact_dir = artifact_dir.resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.output_limit = output_limit
        self.file_limit = file_limit
        self._command_lock = threading.Lock()
        self._stream = None

    @classmethod
    def create(cls, name: str, image: str, artifact_dir: Path, **kwargs) -> "SmolSandbox":
        if not re.fullmatch(r"condere-[a-z0-9-]{1,60}", name):
            raise ValueError("Machine name must be a task-owned Condere identifier.")
        machine = Machine.create(
            MachineConfig(
                name=name,
                image=image,
                command=["sleep", "infinity"],
                labels={"application": "condere", "network": "offline"},
                resources=ResourceSpec(cpus=2, memory_mb=1024),
                network=False,
                mounts=[],
                ports=[],
                env={},
                credentials=[],
                persistent=True,
            ),
            ConnectOptions(target="local"),
        )
        result = machine.exec(["mkdir", "-p", "/workspace"], ExecOptions(timeout=15))
        if result.exit_code != 0:
            machine.stop()
            raise RuntimeError("Sandbox workspace initialization failed; retain its identity.")
        return cls(machine, artifact_dir, **kwargs)

    @classmethod
    def reconnect(cls, name: str, artifact_dir: Path, **kwargs) -> "SmolSandbox":
        if not re.fullmatch(r"condere-[a-z0-9-]{1,60}", name):
            raise ValueError("Invalid Condere machine identifier.")
        return cls(Machine.connect(name, ConnectOptions(target="local")), artifact_dir, **kwargs)

    @property
    def id(self) -> str:
        return self.machine.id

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
                with self.machine.exec_stream(
                    ["/bin/sh", "-c", command],
                    ExecOptions(timeout=timeout, workdir="/workspace"),
                ) as stream:
                    self._stream = stream
                    for event in stream:
                        if event["kind"] in {"stdout", "stderr"}:
                            chunk = event["data"].encode()
                            remaining = self.output_limit - len(data)
                            data.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                truncated, exit_code = True, 125
                                stream.kill()
                                break
                        elif event["kind"] == "exit":
                            exit_code = event["exit_code"]
                        elif event["kind"] == "error":
                            data.extend(b"\nSandbox command failed.\n")
                            exit_code = 1
            except Exception as exc:
                data.extend(f"\nSandbox execution failed ({type(exc).__name__}).\n".encode())
                exit_code = 1
            finally:
                self._stream = None
                artifact.write_bytes(bytes(data[: self.output_limit]))
        text = data.decode(errors="replace")
        return SandboxResult(
            # Filesystem tools parse this output as JSON. The transport cap above
            # bounds memory; Deep Agents offloads large execute-tool output itself.
            output=text,
            exit_code=exit_code,
            truncated=truncated,
            artifact_path=str(artifact),
        )

    @staticmethod
    def _path(path: str) -> PurePosixPath:
        parsed = PurePosixPath(path)
        if (
            "\x00" in path
            or ".." in parsed.parts
            or not any(
                parsed.is_relative_to(root) and parsed != PurePosixPath(root)
                for root in ("/workspace", "/tmp")
            )
        ):
            raise ValueError("Transfers must stay within /workspace or /tmp.")
        return parsed

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for path, content in files:
            try:
                parsed = self._path(path)
                if len(content) > self.file_limit:
                    results.append(FileUploadResponse(path=path, error="file_too_large"))
                    continue
                result = self.machine.exec(
                    ["mkdir", "-p", str(parsed.parent)], ExecOptions(timeout=15)
                )
                if result.exit_code != 0:
                    raise OSError("Cannot prepare guest directory.")
                self.machine.write_file(path, content)
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
                self._path(path)
                response = self.machine.exec(
                    ["python", "-c", _READ_FILE, path, str(self.file_limit)],
                    ExecOptions(timeout=15),
                )
                if response.exit_code != 0:
                    raise OSError("Guest download failed.")
                payload = json.loads(response.stdout)
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

    def cancel(self) -> None:
        stream = self._stream
        if stream is not None:
            stream.kill()

    def close(self, *, delete: bool = False) -> None:
        self.cancel()
        self.machine.stop()
        if delete:
            self.machine.delete()
