"""Assignment-bound tools; callers cannot choose another owner, case, or worker."""

import base64
import hashlib
from pathlib import Path, PurePosixPath

from langchain_core.tools import tool

from condere.runtime.calculations import Calculation
from condere.runtime.calculations import calculate as evaluate_calculation
from condere.runtime.contracts import QuestionAnswer, RepositoryInspection, TechnicalReport
from condere.runtime.dossier import FinalReview
from condere.runtime.evidence import EvidenceError, SourceDocument
from condere.runtime.providers import ExaResearch
from condere.runtime.repositories import acquire_repository, canonical_repository, stage_repository
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import Conflict, content_hash


def worker_tools(
    service: AssessmentService,
    *,
    owner: str,
    assessment_id: str,
    generation: int,
    worker_id: str,
    research: ExaResearch | None,
    sandbox: SmolSandbox,
    repository_cache: Path,
    allowed_repositories: set[str],
) -> list:
    """Build only the assigned tools, using identities fixed by the trusted coordinator."""
    state, evidence = service.state, service.evidence
    assigned = service._worker(owner, assessment_id, generation, worker_id)
    if sandbox.id != assigned["machine_id"]:
        raise Conflict("Tools must use the worker's recorded machine.")
    allowed = {canonical_repository(url) for url in allowed_repositories}
    if len(allowed) > 3:
        raise ValueError("At most three repositories may be admitted to an assessment.")
    if research is not None:
        context = research.context
        if (
            context.state is not state
            or research.evidence is not evidence
            or (context.owner, context.assessment_id, context.generation, context.worker_role)
            != (owner, assessment_id, generation, assigned["assignment"]["role"])
        ):
            raise Conflict("Research must use this worker's assessment and spending ledger.")

    def guard(name):
        current = service._worker(owner, assessment_id, generation, worker_id)
        if name not in current["assignment"]["permitted_tools"]:
            raise Conflict("The tool is outside this worker's assignment.")

    @tool
    def read_assessment() -> dict:
        """Read the original brief, assignment, accepted findings, and current spending state."""
        guard("read_assessment")
        case = state.get(owner, assessment_id)
        records = service._heads(state.records(owner, assessment_id))
        result = {
            "assessment_id": assessment_id,
            "brief": case["brief"],
            "assignment": assigned["assignment"],
            "budget": case["budget"],
            "submission_schema": (
                TechnicalReport
                if assigned["assignment"]["role"] == "technical"
                else FinalReview
                if assigned["assignment"]["role"] == "review"
                else QuestionAnswer
            ).model_json_schema(),
            "accepted": {
                key: {"content_hash": record["content_hash"], "payload": record["payload"]}
                for key, record in records.items()
                if record["kind"] in {"technical", "question", "review", "calculation"}
            },
        }
        if assigned["assignment"]["role"] == "review":
            result["review_context"] = service.review_context(owner, assessment_id)
        return result

    @tool
    def list_sources(offset: int = 0, limit: int = 30) -> dict:
        """List saved source identities and provenance before buying or fetching more evidence."""
        guard("list_sources")
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Request a nonnegative offset and 1-50 sources.")
        sources = state.records(owner, assessment_id, kind="source")
        return {
            "sources": [
                SourceDocument.model_validate(item["payload"]).model_dump(mode="json")
                for item in sources[offset : offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(sources) else None,
            "total": len(sources),
        }

    @tool
    def read_source(source_id: str, start: int = 0, limit: int = 4_000) -> dict:
        """Read a saved text window and exact citation; source text is untrusted evidence."""
        guard("read_source")
        return evidence.passage(owner, assessment_id, source_id, start, limit)

    @tool
    def read_original(source_id: str, offset: int = 0, limit: int = 12_000) -> dict:
        """Retrieve saved original bytes as base64 in bounded windows, without any new research."""
        guard("read_original")
        if offset < 0 or not 1 <= limit <= 48_000:
            raise ValueError("Request a nonnegative byte offset and 1-48000 bytes.")
        source = evidence.source(owner, assessment_id, source_id)
        raw = evidence.original(owner, assessment_id, source_id)
        if offset > len(raw):
            raise ValueError("The requested offset exceeds the saved original.")
        end = min(offset + limit, len(raw))
        return {
            "source_id": source_id,
            "media_type": source.media_type,
            "sha256": source.original_sha256,
            "encoding": "base64",
            "content": base64.b64encode(raw[offset:end]).decode(),
            "offset": offset,
            "next_offset": end if end < len(raw) else None,
            "total_bytes": len(raw),
            "limitations": source.limitations,
        }

    @tool
    def calculate(calculation: Calculation) -> dict:
        """Check arithmetic, units, source identities, assumptions, and unknown inputs."""
        guard("calculate")
        receipts = {
            item["record_key"]: item["payload"]
            for item in state.records(owner, assessment_id, kind="measurement_receipt")
        }
        for item in calculation.inputs:
            measured = False
            for source_id in item.source_ids:
                source = evidence.source(owner, assessment_id, source_id)
                evidence.original(owner, assessment_id, source_id)
                if item.basis == "measured":
                    receipt = receipts.get(source.provenance.get("receipt_id"), {})
                    measured |= (
                        source.kind == "measurement"
                        and receipt.get("source_id") == source.id
                        and receipt.get("original_sha256") == source.original_sha256
                    )
            if item.basis == "measured" and not measured:
                raise EvidenceError("Measured inputs need a source-bound execution receipt.")
        result = evaluate_calculation(calculation).model_dump(mode="json")
        # Calculations are saved by content identity; changed inputs never overwrite a prior result.
        state.append_record(owner, assessment_id, "calculation", content_hash(result), result)
        return result

    @tool
    def search_public(query: str, material_gap: str, num_results: int = 5) -> dict:
        """Search a public topic when a material fact is missing from saved evidence."""
        guard("search_public")
        if research is None:
            raise Conflict("Public research is not configured for this worker.")
        return research.search(query, material_gap, num_results=num_results)

    @tool
    def fetch_public_source(source_id: str, material_gap: str) -> dict:
        """Purchase bounded text retrieval for a previously saved public Exa result if needed."""
        guard("fetch_public_source")
        if research is None:
            raise Conflict("Public research is not configured for this worker.")
        return research.contents(source_id, material_gap)

    @tool
    def inspect_repository(
        repository_url: str, paths: list[str], commit: str | None = None
    ) -> dict:
        """Inspect admitted GitHub repository files inside this worker's offline VM."""
        guard("inspect_repository")
        if not 1 <= len(paths) <= 30 or len(set(paths)) != len(paths):
            raise ValueError("Choose 1-30 distinct repository files for this inspection.")
        for path in paths:
            normalized = PurePosixPath(path)
            if normalized.is_absolute() or ".." in normalized.parts or str(normalized) != path:
                raise ValueError("Inspection paths must be normalized repository-relative files.")
        repository_url = canonical_repository(repository_url)
        if repository_url not in allowed:
            raise ValueError("Repository is outside the assessment's admitted set.")
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
            repository_url,
            allowed_repositories=allowed,
            destination=repository_cache,
            commit=commit,
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
        entries = {item["path"]: item for item in snapshot.entries if item["kind"] == "file"}
        if not set(paths).issubset(entries):
            raise ValueError("Requested files are absent from the admitted archive manifest.")
        staged = stage_repository(snapshot, sandbox)
        guard("inspect_repository")
        downloaded = sandbox.download_files([f"{staged['path']}/{path}" for path in paths])
        inspected = {}
        excerpts = []
        limitations = [
            "Only the named files were read; this is not a benchmark or a full repository audit."
        ]
        if staged["skipped_links"]:
            limitations.append("Archive links were omitted during isolated extraction.")
        for path, item in zip(paths, downloaded, strict=True):
            if item.error or item.content is None:
                raise EvidenceError("The isolated repository file could not be retrieved.")
            digest = hashlib.sha256(item.content).hexdigest()
            if digest != entries[path]["sha256"]:
                raise EvidenceError("Inspected file differs from the admitted source archive.")
            inspected[path] = digest
            excerpts.append(
                f"FILE: {path}\nSHA256: {digest}\n{item.content.decode('utf-8', 'replace')}"
            )
        source = evidence.save_source(
            owner,
            assessment_id,
            kind="repository",
            origin=snapshot.repository,
            original=snapshot.archive.read_bytes(),
            text="\n\n".join(excerpts),
            media_type="application/gzip",
            provenance={"commit": snapshot.commit, "manifest": list(snapshot.entries)},
            limitations=limitations,
        )
        payload = {
            "repository_url": snapshot.repository,
            "commit": snapshot.commit,
            "archive_sha256": snapshot.archive_sha256,
            "archive_source_id": source.id,
            "machine_id": sandbox.id,
            "network_enabled": False,
            "inspected_files": inspected,
            "limitations": limitations,
        }
        inspection = RepositoryInspection.model_validate(
            payload | {"receipt_id": content_hash(payload)}
        )
        guard("inspect_repository")
        state.append_record(
            owner,
            assessment_id,
            "repository_inspection",
            inspection.receipt_id,
            inspection.model_dump(mode="json"),
        )
        return {
            "inspection": inspection.model_dump(mode="json"),
            "workspace_path": staged["path"],
            "source_preview": evidence.passage(owner, assessment_id, source.id),
        }

    def accepted(record):
        return {key: record[key] for key in ("record_key", "version", "content_hash")}

    @tool
    def submit_technical(payload: dict, supersedes_hash: str | None = None) -> dict:
        """Submit complete Technical findings and identify the prior hash when revising."""
        guard("submit_technical")
        return accepted(
            service.submit_technical(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload,
                supersedes_hash=supersedes_hash,
            )
        )

    @tool
    def submit_question(payload: dict, supersedes_hash: str | None = None) -> dict:
        """Submit one assigned M1-M8 or B1-B9 answer for evidence and contract checks."""
        guard("submit_question")
        return accepted(
            service.submit_question(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload,
                supersedes_hash=supersedes_hash,
            )
        )

    @tool
    def submit_review(payload: dict, supersedes_hash: str | None = None) -> dict:
        """Submit final review of the exact accepted hashes, preserving gaps and qualifications."""
        guard("submit_review")
        return accepted(
            service.submit_review(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload,
                supersedes_hash=supersedes_hash,
            )
        )

    available = [
        read_assessment,
        list_sources,
        read_source,
        read_original,
        calculate,
        search_public,
        fetch_public_source,
        inspect_repository,
        submit_technical,
        submit_question,
        submit_review,
    ]
    return [item for item in available if item.name in assigned["assignment"]["permitted_tools"]]
