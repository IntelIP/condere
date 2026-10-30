"""Assignment-bound tools; callers cannot choose another owner, case, or worker."""

import base64
import hashlib
import json
import re
from datetime import date
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from langchain_core.tools import ToolException
from langchain_core.tools import tool as langchain_tool
from pydantic import ValidationError

from condere.runtime.calculations import Calculation
from condere.runtime.calculations import calculate as evaluate_calculation
from condere.runtime.contracts import (
    QUESTIONS,
    OpportunityScreen,
    QuestionAnswer,
    RepositoryInspection,
)
from condere.runtime.dossier import FinalReview
from condere.runtime.evidence import EvidenceError, SourceDocument
from condere.runtime.indexing import pinned_snapshot
from condere.runtime.providers import ApifyResearch, ExaResearch
from condere.runtime.repositories import canonical_repository, stage_repository
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import Conflict, NotFound, content_hash

# Bounded correction: a worker may fix rejected submissions, but not loop indefinitely.
MAX_REJECTED_SUBMISSIONS = 6
SUBMISSION_TOOLS = {"submit_screen", "submit_question", "submit_review"}


class CorrectionLimitReached(RuntimeError):
    """Stops a worker whose submissions keep failing; accepted records and drafts remain."""


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
    social: ApifyResearch | None = None,
) -> list:
    """Build only the assigned tools, using identities fixed by the trusted coordinator."""
    state, evidence = service.state, service.evidence
    assigned = service._worker(owner, assessment_id, generation, worker_id)
    if sandbox.id != assigned["machine_id"]:
        raise Conflict("Tools must use the worker's recorded machine.")
    allowed = {canonical_repository(url) for url in allowed_repositories}
    if research is not None:
        context = research.context
        if (
            context.state is not state
            or research.evidence is not evidence
            or (context.owner, context.assessment_id, context.generation, context.worker_role)
            != (owner, assessment_id, generation, assigned["assignment"]["role"])
        ):
            raise Conflict("Research must use this worker's assessment and spending ledger.")
    if social is not None:
        context = social.context
        if (
            context.state is not state
            or social.evidence is not evidence
            or (context.owner, context.assessment_id, context.generation, context.worker_role)
            != (owner, assessment_id, generation, assigned["assignment"]["role"])
        ):
            raise Conflict("Social research must use this worker's assessment and ledger.")

    rejected = []

    def reject(name: str, detail: str, draft) -> None:
        """Save each rejected draft for review and the dataset, then enforce the bound."""
        rejected.append(name)
        state.append_record(
            owner,
            assessment_id,
            "rejected_submission",
            str(uuid4()),
            {"worker_id": worker_id, "tool": name, "errors": detail, "draft": draft},
        )
        if len(rejected) >= MAX_REJECTED_SUBMISSIONS:
            raise CorrectionLimitReached(
                f"Stopped after {len(rejected)} rejected submissions; drafts are saved."
            )

    def guard(name):
        current = service._worker(owner, assessment_id, generation, worker_id)
        if name not in current["assignment"]["permitted_tools"]:
            raise Conflict("The tool is outside this worker's assignment.")

    def tool(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ValueError, EvidenceError, NotFound, Conflict) as error:
                # Cancellation/stale work must propagate; a running worker may correct bad input.
                service._worker(owner, assessment_id, generation, worker_id)
                detail = (
                    json.dumps(error.errors(include_input=False, include_context=False))
                    if isinstance(error, ValidationError)
                    else str(error)
                )
                if function.__name__ in SUBMISSION_TOOLS:
                    draft = kwargs.get("payload")
                    draft = (
                        draft.model_dump(mode="json")
                        if hasattr(draft, "model_dump")
                        else draft or {"path": kwargs.get("path")}
                    )
                    reject(function.__name__, detail, draft)
                raise ToolException(detail) from error

        def invalid_arguments(error):
            detail = json.dumps(error.errors(include_input=False, include_context=False))
            if function.__name__ in SUBMISSION_TOOLS:
                # Keep the rejected payload itself: failures are dataset material too.
                draft = next(
                    (
                        item["input"]
                        for item in error.errors(include_url=False, include_context=False)
                        if tuple(item["loc"])[:1] == ("payload",)
                        and isinstance(item["input"], dict)
                    ),
                    None,
                )
                reject(function.__name__, detail, draft)
            return detail

        registered = langchain_tool(invoke)
        registered.handle_tool_error = True
        registered.handle_validation_error = invalid_arguments
        return registered

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
                OpportunityScreen
                if assigned["assignment"]["role"] == "screen"
                else FinalReview
                if assigned["assignment"]["role"] == "review"
                else QuestionAnswer
            ).model_json_schema(),
            "question_contracts": {
                identity: QUESTIONS[identity]
                for identity in assigned["assignment"]["question_ids"]
                if assigned["assignment"]["role"] != "review"
            },
            "repository_indexes": [
                {
                    key: item["payload"][key]
                    for key in ("repository", "commit", "source_id", "workspace_path")
                }
                for item in state.records(owner, assessment_id, kind="repository_index")
            ],
            "accepted": {
                key: {"content_hash": record["content_hash"], "payload": record["payload"]}
                for key, record in records.items()
                if record["kind"] in {"screen", "technical", "question", "review", "calculation"}
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
    def search_saved_sources(terms: list[str], limit: int = 5) -> dict:
        """Find relevant passages in this assessment's saved sources without new research.

        Use 1-5 exact words or phrases. Results include exact citations and source
        identities; retrieve surrounding text if it could change the claim.
        """
        guard("search_saved_sources")
        if not 1 <= len(terms) <= 5 or any(not term.strip() or len(term) > 120 for term in terms):
            raise ValueError("Use 1-5 nonempty terms of at most 120 characters.")
        if not 1 <= limit <= 10:
            raise ValueError("Request 1-10 source passages.")
        pattern = re.compile("|".join(re.escape(term.strip()) for term in terms), re.IGNORECASE)
        matches = []
        for record in state.records(owner, assessment_id, kind="source"):
            source = SourceDocument.model_validate(record["payload"])
            text = evidence.text(owner, assessment_id, source.id)
            match = pattern.search(text)
            if match:
                start = max(0, match.start() - 300)
                matches.append(evidence.passage(owner, assessment_id, source.id, start, 1600))
                if len(matches) == limit:
                    break
        return {
            "matches": matches,
            "limit": limit,
            "scope": "Saved sources only; no match does not establish absence of evidence.",
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
    def search_public(
        query: str,
        material_gap: str,
        num_results: int = 10,
        depth: Literal["auto", "deep-lite", "deep"] = "auto",
        additional_queries: list[str] | None = None,
        published_after: date | None = None,
        published_before: date | None = None,
        category: Literal[
            "news", "company", "publication", "personal site", "financial report", "people"
        ]
        | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        fresh: bool = False,
        content: Literal["highlights", "text"] = "highlights",
    ) -> dict:
        """Search public sources when a material fact is missing from saved evidence.

        For anything time-sensitive (launches, pricing, funding, adoption, complaints),
        set published_after and use fresh=True so pages are fetched live, not from cache.
        Route by question: include_domains such as reddit.com, news.ycombinator.com or x.com
        for user pain and workarounds, github.com for adoption, g2.com for buyer complaints;
        category "news" for launches and funding, "company" for vendors. Use depth
        "deep-lite" or "deep" with additional_queries (other phrasings) for broad questions.
        Results return query-focused highlights with title, URL and publication date; call
        fetch_public_url for a page's full text and outgoing links.
        """
        guard("search_public")
        if research is None:
            raise Conflict("Public research is not configured for this worker.")
        return research.search(
            query,
            material_gap,
            num_results=num_results,
            depth=depth,
            additional_queries=tuple(additional_queries or ()),
            published_after=published_after,
            published_before=published_before,
            category=category,
            include_domains=tuple(include_domains or ()),
            exclude_domains=tuple(exclude_domains or ()),
            fresh=fresh,
            content=content,
        )

    @tool
    def fetch_public_source(source_id: str, material_gap: str) -> dict:
        """Fetch the live full text and outgoing links of a saved public web source."""
        guard("fetch_public_source")
        if research is None:
            raise Conflict("Public research is not configured for this worker.")
        return research.contents(source_id, material_gap)

    @tool
    def fetch_public_url(url: str, material_gap: str) -> dict:
        """Fetch a public page live: one from the brief, a saved source, or a saved link.

        Follow links one hop from strong sources. Each saved source lists its outgoing
        links; any of those, or any URL in the brief, may be fetched.
        """
        guard("fetch_public_url")
        if research is None:
            raise Conflict("Public research is not configured for this worker.")
        admitted = {
            str(item)
            for item in state.get(owner, assessment_id)["brief"].get("public_references", [])
        }
        for record in state.records(owner, assessment_id, kind="source"):
            source = SourceDocument.model_validate(record["payload"])
            if source.kind == "web":
                admitted.add(source.origin)
                admitted.update(source.provenance.get("links") or [])
        if url not in admitted:
            raise ValueError(
                "Fetch only URLs from the brief, saved sources, or their saved outgoing links."
            )
        return research.fetch(url, material_gap)

    def require_social():
        if social is None:
            raise Conflict("Social research is not configured for this worker.")
        return social

    @tool
    def x_posts(posts: list[str], material_gap: str) -> dict:
        """Read specific tweets by ID or x.com status URL, such as one supplied in the brief.

        Each result names its conversation; call x_conversation for the replies.
        """
        guard("x_posts")
        return require_social().x_posts(tuple(posts), material_gap)

    @tool
    def x_conversation(conversation_id: str, material_gap: str, max_items: int = 30) -> dict:
        """Read the replies and thread under a tweet, by its conversation ID."""
        guard("x_conversation")
        return require_social().x_conversation(conversation_id, material_gap, max_items=max_items)

    @tool
    def x_search(
        query: str,
        material_gap: str,
        since: date | None = None,
        max_items: int = 30,
        sort: Literal["Top", "Latest"] = "Top",
    ) -> dict:
        """Search X for what practitioners say now: launches, complaints, workarounds.

        Supports X search syntax (quotes, OR, -exclusions, lang:en). Set since for recency.
        """
        guard("x_search")
        return require_social().x_search(
            query, material_gap, since=since, max_items=max_items, sort=sort
        )

    @tool
    def reddit_research(
        material_gap: str,
        thread_urls: list[str] | None = None,
        query: str | None = None,
        community: str | None = None,
        posted_after: date | None = None,
        max_posts: int = 10,
        comments_per_post: int = 10,
    ) -> dict:
        """Read Reddit threads or search posts with top comments: user pain, costs, switching.

        Use community (for example r/selfhosted) to stay in the relevant audience.
        """
        guard("reddit_research")
        return require_social().reddit(
            material_gap,
            thread_urls=tuple(thread_urls or ()),
            query=query,
            community=community,
            posted_after=posted_after,
            max_posts=max_posts,
            comments_per_post=comments_per_post,
        )

    @tool
    def youtube_transcripts(videos: list[str], material_gap: str) -> dict:
        """Transcribe up to 5 YouTube videos: demos, talks and walkthroughs of a capability."""
        guard("youtube_transcripts")
        return require_social().youtube_transcripts(tuple(videos), material_gap)

    @tool
    def inspect_repository(
        repository_url: str, paths: list[str] | None = None, commit: str | None = None
    ) -> dict:
        """Stage a pinned repository offline. Omit paths for its index, then read named files."""
        guard("inspect_repository")
        paths = paths or []
        if len(paths) > 30 or len(set(paths)) != len(paths):
            raise ValueError("Choose at most 30 distinct repository files for this inspection.")
        for path in paths:
            normalized = PurePosixPath(path)
            if normalized.is_absolute() or ".." in normalized.parts or str(normalized) != path:
                raise ValueError("Inspection paths must be normalized repository-relative files.")
        repository_url = canonical_repository(repository_url)
        basis = (
            {"basis": "reference supplied in the admitted brief"}
            if repository_url in allowed
            else None
        )
        if basis is None:
            for record in state.records(owner, assessment_id, kind="source"):
                source = SourceDocument.model_validate(record["payload"])
                parsed = urlsplit(source.origin)
                parts = parsed.path.strip("/").split("/")
                if (
                    source.kind == "web"
                    and source.provenance.get("provider") == "exa"
                    and parsed.hostname == "github.com"
                    and len(parts) >= 2
                    and canonical_repository("https://github.com/" + "/".join(parts[:2]))
                    == repository_url
                ):
                    basis = {"basis": "saved public research result", "source_id": source.id}
                    break
        if basis is None:
            raise ValueError("Repository is outside the brief and saved public research results.")
        snapshot = pinned_snapshot(
            state, owner, assessment_id, generation, repository_url, basis, repository_cache, commit
        )
        entries = {item["path"]: item for item in snapshot.entries if item["kind"] == "file"}
        if not set(paths).issubset(entries):
            raise ValueError("Requested files are absent from the admitted archive manifest.")
        staged = stage_repository(snapshot, sandbox)
        guard("inspect_repository")
        if not paths:
            manifest_path = f"{staged['path']}.manifest.json"
            transferred = sandbox.upload_files(
                [(manifest_path, json.dumps(list(snapshot.entries), sort_keys=True).encode())]
            )
            if transferred[0].error:
                raise EvidenceError("The repository directory index could not be transferred.")
            return {
                **staged,
                "manifest_path": manifest_path,
                "file_count": len(entries),
                "files": list(entries)[:100],
                "truncated": len(entries) > 100,
                "guidance": "Use code-exploration, then inspect named files for citations.",
            }
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
    def submit_screen(
        payload: OpportunityScreen | None = None,
        supersedes_hash: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Save qualification and executable next checks as a payload OR sandbox JSON file.

        This does not claim the full assessment is complete. A deepen recommendation
        proceeds automatically through the existing research stages.
        """
        guard("submit_screen")
        if path is not None:
            if payload is not None:
                raise ValueError("Supply exactly one of payload or path.")
            downloaded = sandbox.download_files([path])[0]
            if downloaded.error or downloaded.content is None:
                raise ValueError("Cannot read screening JSON from the assigned sandbox.")
            payload = OpportunityScreen.model_validate_json(downloaded.content)
        elif payload is None:
            raise ValueError("Supply exactly one of payload or path.")
        return accepted(
            service.submit_screen(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload.model_dump(mode="json"),
                supersedes_hash=supersedes_hash,
            )
        )

    @tool
    def submit_question(
        payload: QuestionAnswer | None = None,
        supersedes_hash: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Submit your assigned question's answer as a payload OR a sandbox JSON file path.

        Cite with source_id and the exact quoted text; the application fills in hashes
        and offsets. Identify the prior hash when revising an accepted answer.
        """
        guard("submit_question")
        if path is not None:
            if payload is not None:
                raise ValueError("Supply exactly one of payload or path.")
            downloaded = sandbox.download_files([path])[0]
            if downloaded.error or downloaded.content is None:
                raise ValueError("Cannot read the answer JSON from the assigned sandbox.")
            payload = QuestionAnswer.model_validate_json(downloaded.content)
        elif payload is None:
            raise ValueError("Supply exactly one of payload or path.")
        return accepted(
            service.submit_question(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload.model_dump(mode="json"),
                supersedes_hash=supersedes_hash,
            )
        )

    @tool
    def submit_review(payload: FinalReview, supersedes_hash: str | None = None) -> dict:
        """Submit final review of the exact accepted hashes, preserving gaps and qualifications."""
        guard("submit_review")
        return accepted(
            service.submit_review(
                owner,
                assessment_id,
                generation,
                worker_id,
                payload.model_dump(mode="json"),
                supersedes_hash=supersedes_hash,
            )
        )

    available = [
        read_assessment,
        search_saved_sources,
        submit_screen,
        list_sources,
        read_source,
        read_original,
        calculate,
        search_public,
        fetch_public_source,
        fetch_public_url,
        x_posts,
        x_conversation,
        x_search,
        reddit_research,
        youtube_transcripts,
        inspect_repository,
        submit_question,
        submit_review,
    ]
    return [item for item in available if item.name in assigned["assignment"]["permitted_tools"]]
