"""Index admitted repositories before any worker runs: deterministic, offline, no model call.

The coordinator runs this once per admitted repository. Its outputs become citable sources
(`repository_index`, `repository_metadata`) and are copied into every worker's sandbox.
"""

import io
import json
import shlex
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from condere.runtime.evidence import EvidenceStore
from condere.runtime.repositories import (
    RepositorySnapshot,
    acquire_repository,
    canonical_repository,
    stage_repository,
)
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.state import AssessmentStore, Conflict, content_hash

INDEXER = Path(__file__).with_name("index_repository.py")
INDEX_FILES = ("facts.json", "report.md", "graph.json")
MAX_INDEX_FILE_BYTES = 64 * 1024 * 1024


def pinned_snapshot(
    state: AssessmentStore,
    owner: str,
    assessment_id: str,
    generation: int,
    repository_url: str,
    basis: dict,
    cache: Path,
    commit: str | None = None,
) -> RepositorySnapshot:
    """Admit within the shared allowance of three and reuse the assessment's pinned commit."""
    repository_url = canonical_repository(repository_url)
    admitted = state.admit_repository(owner, assessment_id, generation, repository_url, basis)
    pin_key = content_hash(repository_url)
    pins = {
        item["record_key"]: item["payload"]
        for item in state.records(owner, assessment_id, kind="repository_pin")
    }
    pinned = pins.get(pin_key)
    if pinned is not None:
        if commit is not None and commit != pinned["commit"]:
            raise Conflict("This assessment has already pinned a different repository commit.")
        commit = pinned["commit"]
    snapshot = acquire_repository(
        repository_url, allowed_repositories=admitted, destination=cache, commit=commit
    )
    if pinned is None:
        try:
            state.append_record(
                owner,
                assessment_id,
                "repository_pin",
                pin_key,
                {
                    "repository": snapshot.repository,
                    "commit": snapshot.commit,
                    "archive_sha256": snapshot.archive_sha256,
                },
            )
        except Conflict:
            raise Conflict(
                "Another worker pinned this repository; invoke again to use its saved identity."
            ) from None
    return snapshot


def workspace_path(snapshot: RepositorySnapshot) -> str:
    return f"/workspace/index/{snapshot.identity}"


def build_index(snapshot: RepositorySnapshot, sandbox: SmolSandbox) -> dict[str, bytes]:
    """Run the indexer inside an offline VM and return its output files."""
    staged = stage_repository(snapshot, sandbox)
    script = "/workspace/indexer.py"
    sandbox.machine.write_file(script, INDEXER.read_bytes())
    output = f"/workspace/index-output/{snapshot.identity}"
    command = " ".join(
        shlex.quote(item)
        for item in [
            "python",
            script,
            staged["path"],
            output,
            snapshot.repository,
            snapshot.commit,
        ]
    )
    response = sandbox.execute(command, timeout=300)
    if response.exit_code != 0:
        raise RuntimeError("Repository indexing failed inside SmolVM: " + response.output[-800:])
    files = {}
    for name in INDEX_FILES:
        path = f"{output}/{name}"
        size = sandbox.execute(f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo missing")
        if size.output.strip() == "missing":
            continue
        if int(size.output.strip()) > MAX_INDEX_FILE_BYTES:
            raise RuntimeError(f"Index output {name} exceeds its size bound.")
        files[name] = sandbox.machine.read_file(path)
    if "facts.json" not in files or "report.md" not in files:
        raise RuntimeError("Repository indexing produced no facts or report.")
    return files


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 0
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def unpack(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        return {
            member.name: bundle.extractfile(member).read()
            for member in bundle.getmembers()
            if member.isfile() and member.name in INDEX_FILES
        }


def github_metadata(repository: str, client: httpx.Client | None = None) -> dict:
    """Dated public GitHub metadata for project-health work; unauthenticated and read-only."""
    owner_repo = canonical_repository(repository).removeprefix("https://github.com/")
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
    base = f"https://api.github.com/repos/{owner_repo}"
    since = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get(path: str):
        response = client.get(base + path, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return response.json()

    try:
        repo = get("")
        releases = get("/releases?per_page=10")
        contributors = get("/contributors?per_page=30")
        commits = get(f"/commits?per_page=100&since={since}")
    finally:
        if owned:
            client.close()
    return {
        "repository": repository,
        "observed_at": datetime.now(UTC).isoformat(),
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "open_issues_and_pull_requests": repo.get("open_issues_count"),
        "created_at": repo.get("created_at"),
        "pushed_at": repo.get("pushed_at"),
        "archived": repo.get("archived"),
        "license": (repo.get("license") or {}).get("spdx_id"),
        "topics": repo.get("topics", []),
        "homepage": repo.get("homepage"),
        "owner_type": (repo.get("owner") or {}).get("type"),
        "recent_releases": [
            {
                "tag": item.get("tag_name"),
                "published_at": item.get("published_at"),
                "prerelease": item.get("prerelease"),
            }
            for item in releases
        ],
        "top_contributors": [
            {"login": item.get("login"), "contributions": item.get("contributions")}
            for item in contributors
        ],
        "commits_last_90_days": len(commits),
        "commits_last_90_days_capped": len(commits) == 100,
        "limitations": [
            "Unauthenticated GitHub API snapshot at observed_at; counts change over time.",
            "Stars and forks measure attention, not adoption.",
            "Contributor list is the API's top 30 by commits.",
        ],
    }


def save_index(
    evidence: EvidenceStore,
    owner: str,
    assessment_id: str,
    snapshot: RepositorySnapshot,
    files: dict[str, bytes],
) -> dict:
    facts = json.loads(files["facts.json"])
    source = evidence.save_source(
        owner,
        assessment_id,
        kind="repository_index",
        origin=snapshot.repository,
        original=archive(files),
        text=files["report.md"].decode("utf-8", "replace"),
        media_type="application/gzip",
        provenance={
            "commit": snapshot.commit,
            "archive_sha256": snapshot.archive_sha256,
            "indexer_sha256": content_hash(INDEXER.read_text()),
        },
        limitations=[
            "Deterministic static index; verify behavior in the cited source files.",
            *facts.get("limitations", []),
        ],
    )
    return {
        "repository": snapshot.repository,
        "commit": snapshot.commit,
        "archive_sha256": snapshot.archive_sha256,
        "source_id": source.id,
        "workspace_path": workspace_path(snapshot),
        "graph_built": bool(facts.get("graph", {}).get("built")),
    }


def save_metadata(evidence: EvidenceStore, owner: str, assessment_id: str, metadata: dict) -> str:
    body = json.dumps(metadata, indent=2, sort_keys=True)
    source = evidence.save_source(
        owner,
        assessment_id,
        kind="repository_metadata",
        origin=metadata["repository"],
        original=body.encode(),
        text=body,
        media_type="application/json",
        provenance={"provider": "github", "observed_at": metadata["observed_at"]},
        limitations=metadata["limitations"],
    )
    return source.id


def install_index(evidence: EvidenceStore, owner: str, assessment_id: str, record: dict, sandbox):
    """Copy a saved index into a worker's sandbox so it can query the graph directly."""
    files = unpack(evidence.original(owner, assessment_id, record["source_id"]))
    prepared = sandbox.execute("mkdir -p " + shlex.quote(record["workspace_path"]))
    if prepared.exit_code != 0:
        raise RuntimeError("Cannot prepare the repository index directory in the sandbox.")
    for name, data in files.items():
        sandbox.machine.write_file(f"{record['workspace_path']}/{name}", data)
