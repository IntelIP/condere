"""One assessment contract for workers, API, and MCP; retrieval never invokes a model."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from condere.runtime.agent import code_method_version, method_version
from condere.runtime.bundle import artifact_link, build_result, render_artifacts, render_readme
from condere.runtime.contracts import IntakeBrief
from condere.runtime.decision import render_brief
from condere.runtime.evidence import EvidenceError, EvidenceStore, SourceDocument
from condere.runtime.funnel import source_funnel
from condere.runtime.spinoff import (
    Analysis,
    BuildPlan,
    Decision,
    ExtractedClaim,
    SpinoffResult,
)
from condere.runtime.state import AssessmentStore, Conflict, NotFound, content_hash

POLICY_VERSION = "condere-mda-v3"
READ_TOOLS = [
    "read_assessment",
    "list_sources",
    "search_saved_sources",
    "read_source",
    "read_original",
    "calculate",
]
RESEARCH_TOOLS = {
    "lead": [
        "search_public",
        "fetch_public_source",
        "fetch_public_url",
        "x_posts",
        "x_conversation",
        "x_search",
        "reddit_research",
        "youtube_transcripts",
        "inspect_repository",
        "submit_claims",
    ],
    "planner": ["inspect_repository"],
}
SUBMIT_TOOL = {"lead": "submit_analysis", "planner": "submit_build_plan"}
OUTPUT_KEY = {"lead": "analysis:main", "planner": "build_plan:main"}
ROLE_SKILLS = {
    "lead": [
        "asset-understanding",
        "rights-and-ownership",
        "open-core-boundary",
        "buyer-evidence",
        "economics",
        "next-test",
        "search-routing",
        "repository-index",
        "code-exploration",
        "evidence-reconciliation",
    ],
    "planner": ["build-plan", "repository-index", "code-exploration"],
}


def output_keys(assignment: dict) -> list[str]:
    """Records a worker must submit before it can finish."""
    return [OUTPUT_KEY[assignment["role"]]]


class _FixedTimeZip:
    """Same saved result, same export bytes: entries carry a fixed timestamp, not the clock."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive

    def writestr(self, name: str, data) -> None:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
        self.archive.writestr(info, data)


class AssessmentService:
    def __init__(self, state: AssessmentStore, evidence: EvidenceStore, *, execution_mode: str):
        if execution_mode not in {"simulation", "live"}:
            raise ValueError("Select simulation or live execution explicitly.")
        self.state, self.evidence, self.execution_mode = state, evidence, execution_mode
        self.method_version = method_version()
        self.code_method = code_method_version()
        self.policy_version = POLICY_VERSION + ":" + execution_mode

    def _compatible(self, assessment: dict) -> None:
        if (assessment["policy_version"], assessment["method_version"]) != (
            self.policy_version,
            self.method_version,
        ):
            raise Conflict("Continue only with the originally admitted policy and methods.")

    def admit(self, owner: str, campaign_id: str, request_key: str, brief: dict) -> dict:
        original = IntakeBrief.model_validate(brief)
        return self.state.admit(
            owner,
            campaign_id,
            request_key,
            original.model_dump(mode="json"),
            policy_version=self.policy_version,
            method_version=self.method_version,
        )

    def begin(self, owner: str, assessment_id: str, *, resume: bool = False) -> dict:
        self._compatible(self.state.get(owner, assessment_id))
        return self.state.begin(owner, assessment_id, resume=resume)

    @staticmethod
    def _heads(records: list[dict]) -> dict[str, dict]:
        heads = {}
        for record in records:
            if content_hash(record["payload"]) != record["content_hash"]:
                raise Conflict("An accepted record no longer matches its content identity.")
            identity = record["kind"] + ":" + record["record_key"]
            if identity not in heads or record["version"] > heads[identity]["version"]:
                heads[identity] = record
        return heads

    def assign_worker(self, owner: str, assessment_id: str, generation: int, role: str) -> dict:
        """The lead or the planner; each worker's skills and tools are narrow."""
        self._compatible(self.state.get(owner, assessment_id))
        if role not in ROLE_SKILLS:
            raise ValueError("Unknown private worker role.")
        assignment = dict(
            role=role,
            skills=ROLE_SKILLS[role],
            permitted_tools=READ_TOOLS + RESEARCH_TOOLS[role] + [SUBMIT_TOOL[role]],
            output_path=f"/workspace/results/{role}.json",
        )
        return self.state.assign_worker(owner, assessment_id, generation, role, assignment)

    def _worker(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        worker_id: str,
        role: str | None = None,
        *,
        allow_completed: bool = False,
    ) -> dict:
        assessment = self.state.get(owner, assessment_id)
        self._compatible(assessment)
        if assessment["generation"] != generation or assessment["status"] != "running":
            raise Conflict("This worker generation is not running.")
        worker = next(
            (
                item
                for item in self.state.workers(owner, assessment_id)
                if item["id"] == worker_id and item["generation"] == generation
            ),
            None,
        )
        if (
            worker is None
            or worker["status"]
            not in ({"running", "completed"} if allow_completed else {"running"})
            or (role is not None and worker["assignment"]["role"] != role)
        ):
            raise Conflict("The worker does not have this running assignment.")
        return worker

    def submit_analysis(
        self, owner, assessment_id, generation, worker_id, payload, *, supersedes_hash=None
    ) -> dict:
        """Accept the lead's analysis once every quote matches its saved source."""
        self._worker(owner, assessment_id, generation, worker_id, "lead")
        payload = self._expand_candidates(owner, assessment_id, payload)
        analysis = Analysis.model_validate(
            self.evidence.resolve_citations(owner, assessment_id, payload)
        )
        if missing := analysis.unexpanded():
            raise ValueError(f"Claims {missing} reference no saved candidate claim.")
        # The decision model reads a brief written from this analysis; refuse it now, while
        # the lead can still shorten it, rather than after the lead has finished.
        render_brief(analysis, self.state.get(owner, assessment_id)["brief"]["original_input"])
        for claim in analysis.claims:
            for citation in claim.citations:
                self.evidence.verify_citation(owner, assessment_id, citation)
        return self.state.append_record(
            owner,
            assessment_id,
            "analysis",
            "main",
            analysis.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
        )

    def submit_claims(
        self, owner, assessment_id, generation, worker_id, claims: list[dict], *, origin: str
    ) -> dict:
        """Save each verified claim as a candidate the lead can reference by ID.

        Claims are checked one by one; a rejected claim returns its reason so it can be
        fixed, without discarding the accepted ones. Identical claims share one ID, whoever
        saved it first. Code-analyst claims carry the code-analysis method that produced them,
        so a later assessment of the same commit can reuse them.
        """
        self._worker(owner, assessment_id, generation, worker_id, "lead")
        saved = {
            record["record_key"]
            for record in self.state.records(owner, assessment_id, kind="candidate_claim")
        }
        accepted, rejected = [], []
        for index, item in enumerate(claims):
            try:
                claim = ExtractedClaim.model_validate(
                    self.evidence.resolve_citations(owner, assessment_id, item)
                )
                for citation in claim.citations:
                    self.evidence.verify_citation(owner, assessment_id, citation)
            except (ValueError, EvidenceError, NotFound) as error:
                rejected.append({"index": index, "error": str(error)[:500]})
                continue
            payload = claim.model_dump(mode="json") | {"origin": origin}
            if origin == "code-analyst":
                payload["code_method"] = self.code_method
            identity = "X" + content_hash(claim.model_dump(mode="json"))[:10]
            if identity not in saved:
                self.state.append_record(
                    owner,
                    assessment_id,
                    "candidate_claim",
                    identity,
                    payload,
                    generation=generation,
                    worker_id=worker_id,
                )
                saved.add(identity)
            accepted.append({"index": index, "candidate": identity})
        return {"accepted": accepted, "rejected": rejected}

    def candidate_claims(self, owner, assessment_id) -> list[dict]:
        """A compact listing for the lead: no quotes, just what each claim says and cites."""
        heads = self._heads(self.state.records(owner, assessment_id, kind="candidate_claim"))
        return [
            {
                "candidate": record["record_key"],
                "statement": record["payload"]["statement"],
                "status": record["payload"]["status"],
                "published_at": record["payload"].get("published_at"),
                "source_ids": sorted(
                    {item["source_id"] for item in record["payload"].get("citations", [])}
                ),
                "origin": record["payload"].get("origin"),
            }
            | (
                {"reused_from": record["payload"]["reused_from"]}
                if "reused_from" in record["payload"]
                else {}
            )
            for record in sorted(heads.values(), key=lambda item: item["created_at"])
        ]

    def reuse_code_claims(self, owner, assessment_id) -> list[dict]:
        """Copy verified code-analyst claims from the owner's latest compatible assessment.

        For each repository this assessment pinned, the source is the owner's most recent
        completed assessment that pinned the same commit and saved code-analyst claims under
        the same code-analysis method (`agent.code_method_version`) and policy. A claim is
        reused only when every citation is to that repository at that commit. Its sources are
        copied under their original identity, and the claim is re-verified against the copies
        through the normal citation checks. Each copy is recorded once as a `reuse` record.
        """
        done = {
            item["record_key"] for item in self.state.records(owner, assessment_id, kind="reuse")
        }
        reused = []
        for pin in self.state.records(owner, assessment_id, kind="repository_pin"):
            if pin["record_key"] in done:
                continue
            repository, commit = pin["payload"]["repository"], pin["payload"]["commit"]
            for previous in self.state.completed_with_code_claims(
                owner,
                assessment_id,
                repository,
                commit,
                policy_version=self.policy_version,
                code_method=self.code_method,
            ):
                claims = self._reusable_claims(owner, previous, repository, commit)
                if claims:
                    reused.append(
                        self._copy_claims(
                            owner,
                            assessment_id,
                            previous,
                            pin["record_key"],
                            pin["payload"],
                            claims,
                        )
                    )
                    break
        return reused

    def _reusable_claims(self, owner, previous, repository, commit) -> list[dict]:
        heads = self._heads(self.state.records(owner, previous, kind="candidate_claim"))
        sources, reusable = {}, []
        for record in sorted(heads.values(), key=lambda item: item["created_at"]):
            payload = record["payload"]
            citations = payload.get("citations") or []
            if (
                payload.get("origin") != "code-analyst"
                or payload.get("code_method") != self.code_method
                or not citations
            ):
                continue
            for citation in citations:
                source_id = citation["source_id"]
                if source_id not in sources:
                    sources[source_id] = self.evidence.source(owner, previous, source_id)
            if all(
                sources[item["source_id"]].origin == repository
                and sources[item["source_id"]].provenance.get("commit") == commit
                for item in citations
            ):
                reusable.append(payload)
        return reusable

    def _copy_claims(self, owner, assessment_id, previous, key, pin, claims) -> dict:
        existing = {
            item["record_key"]
            for item in self.state.records(owner, assessment_id, kind="candidate_claim")
        }
        copied_sources, candidates, skipped = set(), [], []
        for payload in claims:
            try:
                for source_id in {item["source_id"] for item in payload["citations"]}:
                    if source_id not in copied_sources:
                        self.evidence.copy_source(owner, previous, assessment_id, source_id)
                        copied_sources.add(source_id)
                claim = ExtractedClaim.model_validate(
                    self.evidence.resolve_citations(
                        owner,
                        assessment_id,
                        {
                            name: payload[name]
                            for name in ExtractedClaim.model_fields
                            if name in payload
                        },
                    )
                )
                for citation in claim.citations:
                    self.evidence.verify_citation(owner, assessment_id, citation)
            except (ValueError, EvidenceError, NotFound) as error:
                skipped.append(str(error)[:200])
                continue
            identity = "X" + content_hash(claim.model_dump(mode="json"))[:10]
            if identity not in existing:
                self.state.append_record(
                    owner,
                    assessment_id,
                    "candidate_claim",
                    identity,
                    claim.model_dump(mode="json")
                    | {
                        "origin": "code-analyst",
                        "code_method": self.code_method,
                        "reused_from": previous,
                    },
                )
                existing.add(identity)
            candidates.append(identity)
        record = {
            "repository": pin["repository"],
            "commit": pin["commit"],
            "code_method": self.code_method,
            "reused_from": previous,
            "candidates": candidates,
            "sources": sorted(copied_sources),
            "skipped": skipped,
        }
        self.state.append_record(owner, assessment_id, "reuse", key, record)
        return record

    def reused_code_analysis(self, owner, assessment_id) -> list[dict]:
        """What `reuse_code_claims` copied, for the lead: its claims are already candidates."""
        return [
            {
                "repository": item["payload"]["repository"],
                "commit": item["payload"]["commit"],
                "reused_from": item["payload"]["reused_from"],
                "claims": len(item["payload"]["candidates"]),
                "sources": len(item["payload"]["sources"]),
            }
            for item in self.state.records(owner, assessment_id, kind="reuse")
        ]

    def _expand_candidates(self, owner, assessment_id, payload: dict) -> dict:
        claims = payload.get("claims") if isinstance(payload, dict) else None
        if not isinstance(claims, list):
            return payload
        heads = self._heads(self.state.records(owner, assessment_id, kind="candidate_claim"))
        expanded = []
        for claim in claims:
            reference = claim.get("candidate") if isinstance(claim, dict) else None
            if reference and claim.get("statement") is None:
                record = heads.get("candidate_claim:" + reference)
                if record is None:
                    raise ValueError(f"{claim.get('id')} references unknown candidate {reference}.")
                saved = record["payload"]
                claim = {
                    "id": claim["id"],
                    "candidate": reference,
                    "statement": saved["statement"],
                    "status": saved["status"],
                    "citations": saved.get("citations", []),
                    "origin_groups": saved.get("origin_groups", []),
                }
            expanded.append(claim)
        return payload | {"claims": expanded}

    def submit_build_plan(
        self, owner, assessment_id, generation, worker_id, payload, *, supersedes_hash=None
    ) -> dict:
        """Accept a plan only for the decision's chosen option and the analysed components."""
        self._worker(owner, assessment_id, generation, worker_id, "planner")
        plan = BuildPlan.model_validate(payload)
        saved = self.decision(owner, assessment_id)
        if saved is None:
            raise Conflict("A build plan follows the decision.")
        chosen = saved["decision"]["option"]["value"]
        if plan.option != chosen:
            raise ValueError(f"The build plan must target the chosen option {chosen}.")
        components = {item.id for item in self.analysis(owner, assessment_id).asset.components}
        if unknown := sorted(set(plan.keep_open) - components):
            raise ValueError(f"keep_open names components not in the analysis: {unknown}.")
        return self.state.append_record(
            owner,
            assessment_id,
            "build_plan",
            "main",
            plan.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
        )

    def analysis(self, owner, assessment_id) -> Analysis:
        records = self._heads(self.state.records(owner, assessment_id, kind="analysis"))
        if "analysis:main" not in records:
            raise NotFound("No analysis has been submitted.")
        return Analysis.model_validate(records["analysis:main"]["payload"])

    def _revise(self, owner, assessment_id, kind, payload) -> dict:
        """Append the latest version of a coordinator-owned record, naming its predecessor."""
        heads = self._heads(self.state.records(owner, assessment_id, kind=kind))
        prior = heads.get(kind + ":main")
        return self.state.append_record(
            owner,
            assessment_id,
            kind,
            "main",
            payload,
            supersedes_hash=None if prior is None else prior["content_hash"],
        )

    def analysis_hash(self, owner, assessment_id) -> str:
        records = self._heads(self.state.records(owner, assessment_id, kind="analysis"))
        if "analysis:main" not in records:
            raise NotFound("No analysis has been submitted.")
        return records["analysis:main"]["content_hash"]

    def decision(self, owner, assessment_id) -> dict | None:
        """The saved decision and the analysis hash it was made from, if any."""
        records = self._heads(self.state.records(owner, assessment_id, kind="decision"))
        return records["decision:main"]["payload"] if "decision:main" in records else None

    def record_decision(self, owner, assessment_id, decision, brief: str, analysis_hash: str):
        """Save the exact brief and the decision; a changed analysis supersedes both."""
        self._revise(
            owner, assessment_id, "decision_brief", {"text": brief, "sha256": decision.brief.sha256}
        )
        return self._revise(
            owner,
            assessment_id,
            "decision",
            {"analysis_hash": analysis_hash, "decision": decision.model_dump(mode="json")},
        )

    def finish_worker(
        self, owner: str, assessment_id: str, generation: int, worker_id: str
    ) -> dict:
        worker = self._worker(owner, assessment_id, generation, worker_id, allow_completed=True)
        if worker["status"] == "completed":
            return worker
        keys = output_keys(worker["assignment"])
        heads = self._heads(self.state.records(owner, assessment_id))
        if any(key not in heads for key in keys):
            raise Conflict("The worker has not submitted all assigned outputs.")
        return self.state.finish_worker(
            owner,
            assessment_id,
            generation,
            worker_id,
            {
                "accepted_hashes": {key: heads[key]["content_hash"] for key in keys},
                "output_path": worker["assignment"]["output_path"],
            },
        )

    def _source_index(self, owner, assessment_id) -> list[dict]:
        heads = self._heads(self.state.records(owner, assessment_id, kind="source"))
        index = []
        for record in heads.values():
            source = SourceDocument.model_validate(record["payload"])
            provenance = source.provenance
            index.append(
                {
                    "id": source.id,
                    "kind": source.kind,
                    "origin": source.origin,
                    "title": provenance.get("title"),
                    "published_at": provenance.get("published_at"),
                    "retrieved_at": source.retrieved_at.isoformat(),
                    "provider": provenance.get("provider"),
                    "origin_group": provenance.get("origin_group"),
                    "limitations": source.limitations,
                }
            )
        return sorted(index, key=lambda item: item["id"])

    def funnel(self, owner, assessment_id) -> dict:
        """How saved sources narrow to the claims the options rely on, per research route."""
        records = self.state.records(owner, assessment_id)
        heads = self._heads(records)
        return source_funnel(
            [record["payload"] for key, record in heads.items() if key.startswith("source:")],
            self.state.operations(owner, assessment_id),
            [
                record["payload"]
                for key, record in heads.items()
                if key.startswith("candidate_claim:")
            ],
            self.analysis(owner, assessment_id),
        )

    def finalize_spinoff(self, owner, assessment_id, generation) -> SpinoffResult:
        """Write the artifacts, then atomically publish the compact result and complete."""
        assessment = self.state.get(owner, assessment_id)
        self._compatible(assessment)
        analysis = self.analysis(owner, assessment_id)
        saved = self.decision(owner, assessment_id)
        if saved is None or saved["analysis_hash"] != self.analysis_hash(owner, assessment_id):
            raise Conflict("The decision must be made from the current analysis before delivery.")
        decision = Decision.model_validate(saved["decision"])
        heads = self._heads(self.state.records(owner, assessment_id))
        brief = heads["decision_brief:main"]["payload"]["text"]
        plan = heads.get("build_plan:main")
        build_plan = BuildPlan.model_validate(plan["payload"]) if plan else None
        if build_plan is not None and build_plan.option != decision.option.value:
            build_plan = None  # A plan for an earlier decision's option is not delivered.
        sources = self._source_index(owner, assessment_id)
        links = {}
        for ref, (text, media_type, summary) in render_artifacts(
            analysis, decision, brief, sources, self.funnel(owner, assessment_id)
        ).items():
            link = artifact_link(assessment_id, ref, text, media_type, summary)
            self._revise_key(
                owner, assessment_id, "artifact", ref, link.model_dump() | {"text": text}
            )
            links[ref] = link
        plans = self.state.records(owner, assessment_id, kind="execution_plan")
        result = SpinoffResult.model_validate(
            build_result(
                assessment_id=assessment_id,
                created_at=datetime.now(UTC),
                brief=assessment["brief"],
                repositories=plans[0]["payload"]["repository_candidates"] if plans else [],
                analysis=analysis,
                decision=decision,
                build_plan=build_plan,
                links=links,
                source_count=len(sources),
            )
        )
        expected = {
            key: heads[key]["content_hash"]
            for key in ("analysis:main", "decision:main", "build_plan:main")
            if key in heads and (key != "build_plan:main" or build_plan is not None)
        }
        self.state.publish_dossier(
            owner, assessment_id, generation, result.model_dump(mode="json"), expected
        )
        return result

    def _revise_key(self, owner, assessment_id, kind, key, payload) -> dict:
        heads = self._heads(self.state.records(owner, assessment_id, kind=kind))
        prior = heads.get(f"{kind}:{key}")
        return self.state.append_record(
            owner,
            assessment_id,
            kind,
            key,
            payload,
            supersedes_hash=None if prior is None else prior["content_hash"],
        )

    def artifact(self, owner, assessment_id, ref: str, *, offset: int = 0, limit: int = 20_000):
        """A window of one saved artifact; retrieval never starts research."""
        if offset < 0 or not 1 <= limit <= 100_000:
            raise ValueError("Read 1-100,000 characters from a nonnegative offset.")
        heads = self._heads(self.state.records(owner, assessment_id, kind="artifact"))
        record = heads.get(f"artifact:{ref}")
        if record is None:
            raise NotFound(f"No artifact named {ref}.")
        payload = record["payload"]
        text = payload["text"]
        end = min(offset + limit, len(text))
        return {
            "ref": ref,
            "sha256": payload["sha256"],
            "media_type": payload["media_type"],
            "summary": payload["summary"],
            "total_characters": len(text),
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "text": text[offset:end],
        }

    def report(self, owner: str, assessment_id: str) -> SpinoffResult:
        records = self.state.records(owner, assessment_id, kind="dossier")
        if not records:
            raise NotFound(
                "A final result has not been delivered; accepted partial records remain available."
            )
        payload = self._heads(records)["dossier:final"]["payload"]
        if payload.get("schema_version") != "condere.spinoff.v1":
            raise Conflict(
                "This result uses the retired 26-question schema; read it through commit "
                "e26d4b59 on codex/condere-mda."
            )
        return SpinoffResult.model_validate(payload)

    def _export_spinoff(self, owner, assessment_id, result, archive) -> None:
        """README first, the compact JSON, then every artifact at its linked path."""
        archive.writestr("README.md", render_readme(result))
        archive.writestr("assessment.json", result.model_dump_json(indent=2) + "\n")
        for link in result.artifacts:
            text = self.artifact(owner, assessment_id, link.ref, limit=100_000)
            parts = [text["text"]]
            while text["next_offset"] is not None:
                text = self.artifact(
                    owner, assessment_id, link.ref, offset=text["next_offset"], limit=100_000
                )
                parts.append(text["text"])
            archive.writestr(link.ref, "".join(parts))
        for source in self._source_index(owner, assessment_id):
            archive.writestr(
                f"artifacts/sources/{source['id']}.txt",
                self.evidence.text(owner, assessment_id, source["id"]),
            )

    def export(self, owner: str, assessment_id: str, target: Path) -> Path:
        result = self.report(owner, assessment_id)
        created = False
        try:
            with target.open("xb") as output:
                created = True
                target.chmod(0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    self._export_spinoff(owner, assessment_id, result, _FixedTimeZip(zipped))
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        return target
