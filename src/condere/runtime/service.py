"""One assessment contract for workers, API, and MCP; retrieval never invokes a model."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from condere.runtime.agent import method_version
from condere.runtime.bundle import artifact_link, build_result, render_artifacts
from condere.runtime.calculations import calculate
from condere.runtime.contracts import (
    QUESTION_IDS,
    QUESTIONS,
    STAGES,
    AssessmentFindings,
    IntakeBrief,
    OpportunityScreen,
    QuestionAnswer,
)
from condere.runtime.dossier import (
    AcceptedRecord,
    CheckedCalculation,
    Dossier,
    FinalReview,
    ScreeningResult,
    render_json,
    render_markdown,
)
from condere.runtime.evidence import EvidenceError, EvidenceStore, SourceDocument
from condere.runtime.reporting import render_figures, render_html
from condere.runtime.spinoff import Analysis, BuildPlan, Decision, SpinoffResult
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
    "screen": [
        "search_public",
        "fetch_public_source",
        "fetch_public_url",
        "x_posts",
        "x_conversation",
        "x_search",
        "reddit_research",
        "youtube_transcripts",
        "inspect_repository",
    ],
    "technical": [
        "search_public",
        "fetch_public_source",
        "fetch_public_url",
        "x_posts",
        "x_conversation",
        "x_search",
        "reddit_research",
        "youtube_transcripts",
        "inspect_repository",
    ],
    "market": [
        "search_public",
        "fetch_public_source",
        "fetch_public_url",
        "x_posts",
        "x_conversation",
        "x_search",
        "reddit_research",
        "youtube_transcripts",
    ],
    "business": [
        "search_public",
        "fetch_public_source",
        "fetch_public_url",
        "x_posts",
        "x_conversation",
        "x_search",
        "reddit_research",
        "youtube_transcripts",
    ],
    "review": [],
}
RESEARCH_TOOLS["lead"] = list(RESEARCH_TOOLS["technical"])
RESEARCH_TOOLS["planner"] = ["inspect_repository"]
SUBMIT_TOOL = {
    "screen": "submit_screen",
    "review": "submit_review",
    "lead": "submit_analysis",
    "planner": "submit_build_plan",
}
SHARED_SKILLS = ["opportunity-framing"]
ROLE_SKILLS = {
    "screen": ["repository-index", "code-exploration"],
    "review": ["evidence-reconciliation", "dossier-and-handoff", "visual-reporting"],
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
    if assignment["role"] == "screen":
        return ["screen:main"]
    if assignment["role"] == "review":
        return ["review:final"]
    if assignment["role"] == "lead":
        return ["analysis:main"]
    if assignment["role"] == "planner":
        return ["build_plan:main"]
    return ["question:" + identity for identity in assignment["question_ids"]]


def spending_summary(spending: dict) -> dict:
    """Totals for the dossier; per-operation records stay in the ledger and API status.

    Embedding every operation made a 26-question live dossier ~10 MB (2026-10-28).
    """
    groups: dict[tuple[str, str], dict] = {}
    plan_tokens = 0
    for item in spending["operations"]:
        group = groups.setdefault(
            (item["kind"], item["state"]),
            {
                "kind": item["kind"],
                "state": item["state"],
                "count": 0,
                "actual_microusd": 0,
                "pending_microusd": 0,
            },
        )
        group["count"] += 1
        if item["actual_microusd"] is None:  # Unknown charges stay reserved, never zero.
            group["pending_microusd"] += item["reserved_microusd"]
        else:
            group["actual_microusd"] += item["actual_microusd"]
        if item["kind"] == "chatgpt_plan":
            plan_tokens += ((item["cost_basis"] or {}).get("usage") or {}).get("total_tokens", 0)
    return {
        "budget": spending["budget"],
        "operations": sorted(groups.values(), key=lambda group: (group["kind"], group["state"])),
        "chatgpt_plan_tokens": plan_tokens,
        "detail": "Per-operation records remain in the ledger and the assessment status API.",
    }


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

    def assign_worker(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        role: str,
        question_id: str | None = None,
    ) -> dict:
        """One worker per question (or the screen/review); its skills and tools are narrow."""
        self._compatible(self.state.get(owner, assessment_id))
        if role not in {"screen", "review", "lead", "planner", *STAGES}:
            raise ValueError("Unknown private worker role.")
        if (role in STAGES) != (question_id is not None) or (
            question_id is not None and question_id not in STAGES[role]
        ):
            raise ValueError("Question workers need one question from their own stage.")
        question_ids = (
            list(QUESTION_IDS) if role == "review" else [question_id] if question_id else []
        )
        skills = (
            ROLE_SKILLS[role]
            if role in {"lead", "planner"}
            else SHARED_SKILLS
            + (QUESTIONS[question_id]["skills"] if question_id else ROLE_SKILLS[role])
        )
        key = f"{role}:{question_id}" if question_id else role
        assignment = dict(
            role=role,
            question_ids=question_ids,
            skills=list(dict.fromkeys(skills)),
            permitted_tools=READ_TOOLS
            + RESEARCH_TOOLS[role]
            + [SUBMIT_TOOL.get(role, "submit_question")],
            output_path=f"/workspace/results/{key.replace(':', '-')}.json",
        )
        return self.state.assign_worker(owner, assessment_id, generation, key, assignment)

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

    def submit_screen(
        self, owner, assessment_id, generation, worker_id, payload, *, supersedes_hash=None
    ) -> dict:
        self._worker(owner, assessment_id, generation, worker_id, "screen")
        if self.state.get(owner, assessment_id)["brief"].get("analysis_depth") != "adaptive":
            raise Conflict("Screening belongs to an adaptive assessment.")
        screen = OpportunityScreen.model_validate(
            self.evidence.resolve_citations(owner, assessment_id, payload)
        )
        self.evidence.verify_findings(owner, assessment_id, screen)
        return self.state.append_record(
            owner,
            assessment_id,
            "screen",
            "main",
            screen.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
        )

    def submit_analysis(
        self, owner, assessment_id, generation, worker_id, payload, *, supersedes_hash=None
    ) -> dict:
        """Accept the lead's analysis once every quote matches its saved source."""
        self._worker(owner, assessment_id, generation, worker_id, "lead")
        analysis = Analysis.model_validate(
            self.evidence.resolve_citations(owner, assessment_id, payload)
        )
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

    def screening(self, owner, assessment_id) -> OpportunityScreen:
        records = self._heads(self.state.records(owner, assessment_id, kind="screen"))
        if "screen:main" not in records:
            raise NotFound("No opportunity screen has been submitted.")
        return OpportunityScreen.model_validate(records["screen:main"]["payload"])

    def finalize_screen(self, owner, assessment_id, generation) -> ScreeningResult:
        assessment = self.state.get(owner, assessment_id)
        self._compatible(assessment)
        screen = self.screening(owner, assessment_id)
        if screen.recommendation == "deepen":
            raise Conflict("The selected deeper investigation must finish before delivery.")
        self.evidence.verify_findings(owner, assessment_id, screen)
        heads = self._heads(self.state.records(owner, assessment_id))
        result = ScreeningResult(
            assessment_id=assessment_id,
            execution_mode=self.execution_mode,
            created_at=datetime.now(UTC),
            brief=IntakeBrief.model_validate(assessment["brief"]),
            screen=screen,
            sources=[
                SourceDocument.model_validate(item["payload"])
                for item in heads.values()
                if item["kind"] == "source"
            ],
            spending=spending_summary(self.state.spending(owner, assessment_id)),
            method_version=assessment["method_version"],
            policy_version=assessment["policy_version"],
        )
        self.state.publish_dossier(
            owner,
            assessment_id,
            generation,
            result.model_dump(mode="json"),
            {"screen:main": heads["screen:main"]["content_hash"]},
        )
        return result

    def submit_question(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        worker_id: str,
        payload: dict,
        *,
        supersedes_hash: str | None = None,
    ) -> dict:
        worker = self._worker(owner, assessment_id, generation, worker_id)
        answer = QuestionAnswer.model_validate(
            self.evidence.resolve_citations(owner, assessment_id, payload)
        )
        if (
            answer.question_id not in worker["assignment"]["question_ids"]
            or worker["assignment"]["role"] not in STAGES
        ):
            raise Conflict("This question is outside the worker's assignment.")
        self.evidence.verify_findings(owner, assessment_id, answer)
        return self.state.append_record(
            owner,
            assessment_id,
            "question",
            answer.question_id,
            answer.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
        )

    def findings(
        self, owner: str, assessment_id: str
    ) -> tuple[AssessmentFindings, dict[str, dict]]:
        heads = self._heads(self.state.records(owner, assessment_id))
        needed = {"question:" + identity for identity in QUESTION_IDS}
        if not needed.issubset(heads):
            raise Conflict("All 26 Technical, Market and Business records precede final review.")
        findings = AssessmentFindings(
            questions=[
                QuestionAnswer.model_validate(heads["question:" + identity]["payload"])
                for identity in QUESTION_IDS
            ],
        )
        return findings, heads

    def review_context(self, owner: str, assessment_id: str) -> dict:
        findings, heads = self.findings(owner, assessment_id)
        return {
            "findings": findings.model_dump(mode="json"),
            "reviewed_hashes": {
                key.removeprefix("question:"): record["content_hash"]
                for key, record in heads.items()
                if record["kind"] == "question"
            },
        }

    def _verify_review(
        self,
        owner: str,
        assessment_id: str,
        findings: AssessmentFindings,
        heads: dict[str, dict],
        review: FinalReview,
    ) -> None:
        for item in review.items:
            key = "question:" + item.target
            if heads[key]["content_hash"] != item.reviewed_hash:
                raise Conflict("The review does not cover the current accepted version.")
            for citation in item.citations:
                self.evidence.original(owner, assessment_id, citation.source_id)
                self.evidence.verify_citation(owner, assessment_id, citation)
            answer = next(
                answer for answer in findings.questions if answer.question_id == item.target
            )
            unknown = answer.availability == "unavailable" or any(
                calculate(calculation).value is None for calculation in answer.calculations
            )
            if item.status == "supported" and unknown:
                raise Conflict(
                    "Unavailable evidence must remain qualified or insufficient in review."
                )
        self.evidence.verify_findings(owner, assessment_id, findings)

    def submit_review(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        worker_id: str,
        payload: dict,
        *,
        supersedes_hash: str | None = None,
    ) -> dict:
        self._worker(owner, assessment_id, generation, worker_id, "review")
        findings, heads = self.findings(owner, assessment_id)
        review = FinalReview.model_validate(
            self.evidence.resolve_citations(owner, assessment_id, payload)
        )
        self._verify_review(owner, assessment_id, findings, heads, review)
        return self.state.append_record(
            owner,
            assessment_id,
            "review",
            "final",
            review.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
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

    def finalize(self, owner: str, assessment_id: str, generation: int, *, sandbox=None) -> Dossier:
        assessment = self.state.get(owner, assessment_id)
        if assessment["status"] == "completed":
            return self.report(owner, assessment_id)
        self._compatible(assessment)
        findings, heads = self.findings(owner, assessment_id)
        if "review:final" not in heads:
            raise Conflict("A complete final review is required before delivery.")
        review = FinalReview.model_validate(heads["review:final"]["payload"])
        self._verify_review(owner, assessment_id, findings, heads, review)
        history = self.state.records(owner, assessment_id)
        calculations = [
            CheckedCalculation(target=answer.question_id, result=calculate(item))
            for answer in findings.questions
            for item in answer.calculations
        ]
        dossier = Dossier(
            assessment_id=assessment_id,
            execution_mode=self.execution_mode,
            created_at=datetime.now(UTC),
            brief=IntakeBrief.model_validate(assessment["brief"]),
            findings=findings,
            review=review,
            screening=(self.screening(owner, assessment_id) if "screen:main" in heads else None),
            record_history=[
                AcceptedRecord(
                    kind=item["kind"],
                    key=item["record_key"],
                    version=item["version"],
                    content_hash=item["content_hash"],
                    supersedes_hash=item["supersedes_hash"],
                    payload=item["payload"],
                )
                for item in history
                if item["kind"] in {"screen", "question", "review"}
            ],
            sources=[
                SourceDocument.model_validate(item["payload"])
                for item in history
                if item["kind"] == "source"
            ],
            calculations=calculations,
            spending=spending_summary(self.state.spending(owner, assessment_id)),
            method_version=assessment["method_version"],
            policy_version=assessment["policy_version"],
        )
        if sandbox is not None:
            files = render_figures(dossier, sandbox)
            for asset in dossier.assets:
                digest = self.evidence._put_object(
                    assessment_id, files[asset.path], asset.media_type
                )
                if digest != asset.sha256:
                    raise EvidenceError("The persisted report artifact changed identity.")
        expected = {
            key: record["content_hash"]
            for key, record in heads.items()
            if record["kind"] in {"screen", "question", "review"}
        }
        self.state.publish_dossier(
            owner, assessment_id, generation, dossier.model_dump(mode="json"), expected
        )
        return dossier

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
            analysis, decision, brief, sources
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

    def report(self, owner: str, assessment_id: str) -> Dossier | ScreeningResult | SpinoffResult:
        records = self.state.records(owner, assessment_id, kind="dossier")
        if not records:
            raise NotFound(
                "A final dossier has not been delivered; accepted partial records remain available."
            )
        saved = self._heads(records)["dossier:final"]
        model = {
            "condere.screen.v1": ScreeningResult,
            "condere.spinoff.v1": SpinoffResult,
        }.get(saved["payload"].get("schema_version"), Dossier)
        return model.model_validate(saved["payload"])

    def export(self, owner: str, assessment_id: str, target: Path) -> Path:
        dossier = self.report(owner, assessment_id)
        created = False
        try:
            with target.open("xb") as output:
                created = True
                target.chmod(0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    archive = _FixedTimeZip(zipped)
                    archive.writestr("assessment.json", render_json(dossier))
                    archive.writestr("assessment.md", render_markdown(dossier))
                    archive.writestr("assessment.html", render_html(dossier))
                    for asset in dossier.assets:
                        archive.writestr(
                            asset.path,
                            self.evidence._read_object(assessment_id, asset.sha256, asset.bytes),
                        )
                    for source in dossier.sources:
                        archive.writestr(
                            f"sources/{source.id}.original",
                            self.evidence.original(owner, assessment_id, source.id),
                        )
                        archive.writestr(
                            f"sources/{source.id}.txt",
                            self.evidence.text(owner, assessment_id, source.id),
                        )
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        return target
