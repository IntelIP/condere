"""Acquire admitted public GitHub snapshots without executing repository code on the host."""

import hashlib
import json
import re
import shlex
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from condere.runtime.sandbox import CodeSandbox

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10_000


def canonical_repository(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Repository must be an HTTPS GitHub URL without credentials or query.")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) or part in {".", ".."} for part in parts
    ):
        raise ValueError("Repository URL must contain only owner/repository.")
    return "https://github.com/" + "/".join(part.lower() for part in parts)


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    commit: str
    archive: Path
    archive_sha256: str
    entries: tuple[dict, ...]

    @property
    def identity(self) -> str:
        return hashlib.sha256(f"{self.repository}@{self.commit}".encode()).hexdigest()


def _manifest(archive: Path) -> tuple[dict, ...]:
    entries, seen, total, root = [], set(), 0, None
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("Unsafe path in repository archive.")
            root = root or path.parts[0]
            if path.parts[0] != root:
                raise ValueError("Repository archive has multiple roots.")
            relative = str(PurePosixPath(*path.parts[1:]))
            if relative in seen:
                raise ValueError("Repository archive repeats a path.")
            seen.add(relative)
            total += member.size
            if len(seen) > MAX_ENTRIES or total > MAX_SOURCE_BYTES:
                raise ValueError("Repository exceeds admitted snapshot limits.")
            if member.isdir():
                continue
            if member.isfile():
                handle = source.extractfile(member)
                if handle is None:
                    raise ValueError("Repository archive has no readable content.")
                with handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                entries.append(
                    {"path": relative, "size": member.size, "sha256": digest, "kind": "file"}
                )
            elif member.issym() or member.islnk():
                entries.append({"path": relative, "kind": "link", "target": member.linkname})
            else:
                raise ValueError("Repository archive contains an unsupported special file.")
    if not entries:
        raise ValueError("Repository snapshot is empty.")
    return tuple(entries)


def acquire_repository(
    url: str,
    *,
    allowed_repositories: set[str],
    destination: Path,
    commit: str | None = None,
    client: httpx.Client | None = None,
) -> RepositorySnapshot:
    """Resolve once, retain exact identity, and never follow an arbitrary redirect."""
    allowed = {canonical_repository(item) for item in allowed_repositories}
    repository = canonical_repository(url)
    if len(allowed) > 3 or repository not in allowed:
        raise ValueError("Repository is outside the admitted set of at most three.")
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A pinned commit must be its full 40-character Git identity.")
    owned_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
    owner_repo = repository.removeprefix("https://github.com/")
    try:
        if commit is None:
            with client.stream(
                "GET",
                f"https://api.github.com/repos/{owner_repo}/commits?per_page=1",
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                metadata = bytearray()
                for chunk in response.iter_bytes():
                    metadata.extend(chunk)
                    if len(metadata) > 512 * 1024:
                        raise ValueError("Commit metadata exceeds its limit.")
                commit = json.loads(metadata)[0]["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError("GitHub did not return a full commit identity.")
        identity = hashlib.sha256(f"{repository}@{commit}".encode()).hexdigest()
        location = destination / identity
        location.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = location / "source.tar.gz"
        receipt = location / "snapshot.json"
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if (
                saved["repository"] != repository
                or saved["commit"] != commit
                or _file_hash(archive) != saved["archive_sha256"]
            ):
                raise ValueError("Saved repository snapshot identity is inconsistent.")
            return RepositorySnapshot(
                repository, commit, archive, saved["archive_sha256"], tuple(saved["entries"])
            )
        temporary = archive.with_suffix(".partial")
        try:
            with client.stream(
                "GET",
                f"https://codeload.github.com/{owner_repo}/tar.gz/{commit}",
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                total = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ValueError("Repository download exceeds its limit.")
                        handle.write(chunk)
            entries = _manifest(temporary)
            digest = _file_hash(temporary)
            temporary.replace(archive)
            payload = {
                "repository": repository,
                "commit": commit,
                "archive_sha256": digest,
                "entries": entries,
            }
            pending_receipt = receipt.with_suffix(".partial")
            pending_receipt.write_text(json.dumps(payload, indent=2) + "\n")
            pending_receipt.replace(receipt)
            return RepositorySnapshot(repository, commit, archive, digest, entries)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        if owned_client:
            client.close()


def stage_repository(snapshot: RepositorySnapshot, sandbox: CodeSandbox) -> dict:
    """Transfer the verified archive and unpack only inside the already isolated sandbox."""
    if (
        snapshot.archive.stat().st_size > MAX_ARCHIVE_BYTES
        or _file_hash(snapshot.archive) != snapshot.archive_sha256
    ):
        raise ValueError("Repository archive changed after acquisition.")
    guest_archive = f"/tmp/{snapshot.identity}.tar.gz"
    # Repeated inspection must not reuse a workspace that untrusted code could have changed.
    guest_directory = f"/workspace/repositories/{snapshot.identity}-{uuid4().hex}"
    # Trusted acquisition uses its own bound; agent file tools retain their smaller limit.
    sandbox.write_trusted(guest_archive, snapshot.archive.read_bytes())
    script = """
import json, pathlib, tarfile, sys
destination = pathlib.Path(sys.argv[2])
destination.mkdir(parents=True, exist_ok=False)
skipped = []
def safe_member(member, path):
    if member.issym() or member.islnk():
        skipped.append(member.name)
        return None
    return tarfile.data_filter(member, path)
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    archive.extractall(destination, filter=safe_member)
roots = list(destination.iterdir())
if len(roots) != 1 or not roots[0].is_dir():
    raise ValueError('Unexpected snapshot root')
print(json.dumps({'path': str(roots[0]), 'skipped_links': skipped}))
"""
    command = " ".join(
        shlex.quote(item) for item in ["python", "-c", script, guest_archive, guest_directory]
    )
    response = sandbox.execute(command)
    if response.exit_code != 0:
        raise RuntimeError(
            "Repository extraction failed inside the sandbox; no host fallback was used."
        )
    return {
        **json.loads(response.output),
        "repository": snapshot.repository,
        "commit": snapshot.commit,
        "archive_sha256": snapshot.archive_sha256,
        "machine_id": sandbox.id,
        "extraction_artifact": response.artifact_path,
    }
