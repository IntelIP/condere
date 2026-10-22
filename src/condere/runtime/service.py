"""One assessment contract for workers, API, and MCP; retrieval never invokes a model."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from condere.runtime.agent import method_version
from condere.runtime.calculations import calculate
from condere.runtime.contracts import (
    CAPABILITY_CATEGORIES,
    QUESTION_IDS,
    AssessmentFindings,
    IntakeBrief,
    QuestionAnswer,
    TechnicalReport,
)
from condere.runtime.dossier import (
    AcceptedRecord,
    CheckedCalculation,
    Dossier,
    FinalReview,
    render_json,
    render_markdown,
)
from condere.runtime.evidence import EvidenceStore, SourceDocument
from condere.runtime.state import AssessmentStore, Conflict, NotFound, content_hash

POLICY_VERSION = "condere-mda-v1"
READ_TOOLS = ["read_assessment", "list_sources", "read_source", "read_original", "calculate"]


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
        *,
        required_capabilities: list[dict] | None = None,
    ) -> dict:
        self._compatible(self.state.get(owner, assessment_id))
        if role not in {"technical", "market", "business", "review"}:
            raise ValueError("Unknown private worker role.")
        question_ids = [
            identity
            for identity in QUESTION_IDS
            if role == "review"
            or identity.startswith("M" if role == "market" else "B")
            and role in {"market", "business"}
        ]
        requirements = required_capabilities or [
            dict(scenario="primary", category=category, name=category)
            for category in sorted(CAPABILITY_CATEGORIES)
        ]
        if role != "technical" and required_capabilities is not None:
            raise ValueError("Capability requirements belong to the Technical assignment.")
        if any(
            set(item) != {"scenario", "category", "name"}
            or item["category"] not in CAPABILITY_CATEGORIES
            or not item["scenario"]
            or not item["name"]
            for item in requirements
        ):
            raise ValueError("Every capability requirement needs a scenario, category, and name.")
        tools = READ_TOOLS + ([] if role == "review" else ["search_public", "inspect_repository"])
        tools += [
            "submit_review"
            if role == "review"
            else "submit_technical"
            if role == "technical"
            else "submit_question"
        ]
        assignment = dict(
            role=role,
            question_ids=question_ids,
            required_capabilities=requirements if role == "technical" else [],
            permitted_tools=tools,
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

    def submit_technical(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        worker_id: str,
        payload: dict,
        *,
        supersedes_hash: str | None = None,
    ) -> dict:
        worker = self._worker(owner, assessment_id, generation, worker_id, "technical")
        report = TechnicalReport.model_validate(payload)
        actual = {(item.scenario, item.category, item.name) for item in report.capabilities}
        required = {
            (item["scenario"], item["category"], item["name"])
            for item in worker["assignment"]["required_capabilities"]
        }
        if not required.issubset(actual):
            raise Conflict("Technical output omitted a named capability from its assignment.")
        self.evidence.verify_findings(owner, assessment_id, report)
        return self.state.append_record(
            owner,
            assessment_id,
            "technical",
            "main",
            report.model_dump(mode="json"),
            supersedes_hash=supersedes_hash,
            generation=generation,
            worker_id=worker_id,
        )

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
        answer = QuestionAnswer.model_validate(payload)
        if answer.question_id not in worker["assignment"]["question_ids"] or worker["assignment"][
            "role"
        ] not in {"market", "business"}:
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
        needed = {"technical:main", *("question:" + identity for identity in QUESTION_IDS)}
        if not needed.issubset(heads):
            raise Conflict(
                "All Technical and 17 question records are required before final review."
            )
        findings = AssessmentFindings(
            technical=TechnicalReport.model_validate(heads["technical:main"]["payload"]),
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
                "technical" if key == "technical:main" else key.removeprefix("question:"): record[
                    "content_hash"
                ]
                for key, record in heads.items()
                if record["kind"] in {"technical", "question"}
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
            key = "technical:main" if item.target == "technical" else "question:" + item.target
            if heads[key]["content_hash"] != item.reviewed_hash:
                raise Conflict("The review does not cover the current accepted version.")
            for citation in item.citations:
                self.evidence.original(owner, assessment_id, citation.source_id)
                self.evidence.verify_citation(owner, assessment_id, citation)
            statements = (
                list(findings.technical.findings.values())
                if item.target == "technical"
                else [
                    next(
                        answer for answer in findings.questions if answer.question_id == item.target
                    )
                ]
            )
            unknown = any(statement.availability == "unavailable" for statement in statements)
            if item.target == "technical":
                unknown |= any(
                    capability.status == "unavailable"
                    for capability in findings.technical.capabilities
                )
                unknown |= any(
                    metric.basis == "unavailable" for metric in findings.technical.metrics
                )
            else:
                unknown |= any(
                    calculate(calculation).value is None
                    for calculation in statements[0].calculations
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
        review = FinalReview.model_validate(payload)
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
        role = worker["assignment"]["role"]
        keys = (
            ["technical:main"]
            if role == "technical"
            else ["review:final"]
            if role == "review"
            else ["question:" + identity for identity in worker["assignment"]["question_ids"]]
        )
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

    def finalize(self, owner: str, assessment_id: str, generation: int) -> Dossier:
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
        calculations += [
            CheckedCalculation(
                target="technical:" + metric.name, result=calculate(metric.calculation)
            )
            for metric in findings.technical.metrics
            if metric.calculation
        ]
        dossier = Dossier(
            assessment_id=assessment_id,
            execution_mode=self.execution_mode,
            created_at=datetime.now(UTC),
            brief=IntakeBrief.model_validate(assessment["brief"]),
            findings=findings,
            review=review,
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
                if item["kind"] in {"technical", "question", "review"}
            ],
            sources=[
                SourceDocument.model_validate(item["payload"])
                for item in history
                if item["kind"] == "source"
            ],
            calculations=calculations,
            spending=self.state.spending(owner, assessment_id),
            method_version=assessment["method_version"],
            policy_version=assessment["policy_version"],
        )
        expected = {
            key: record["content_hash"]
            for key, record in heads.items()
            if record["kind"] in {"technical", "question", "review"}
        }
        self.state.publish_dossier(
            owner, assessment_id, generation, dossier.model_dump(mode="json"), expected
        )
        return dossier

    def report(self, owner: str, assessment_id: str) -> Dossier:
        records = self.state.records(owner, assessment_id, kind="dossier")
        if not records:
            raise NotFound(
                "A final dossier has not been delivered; accepted partial records remain available."
            )
        saved = self._heads(records)["dossier:final"]
        return Dossier.model_validate(saved["payload"])

    def export(self, owner: str, assessment_id: str, target: Path) -> Path:
        dossier = self.report(owner, assessment_id)
        created = False
        try:
            with target.open("xb") as output:
                created = True
                target.chmod(0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("assessment.json", render_json(dossier))
                    archive.writestr("assessment.md", render_markdown(dossier))
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
