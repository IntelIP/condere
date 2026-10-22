"""Saved findings, their exact-version review, and deterministic user-facing rendering."""

import json
from decimal import Decimal
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from condere.runtime.calculations import CalculationResult, Record, Text
from condere.runtime.contracts import (
    QUESTION_IDS,
    QUESTIONS,
    AssessmentFindings,
    Citation,
    Finding,
    IntakeBrief,
)
from condere.runtime.evidence import SourceDocument

REVIEW_TARGETS = {"technical", *QUESTION_IDS}


class ReviewItem(Record):
    target: Text
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["supported", "qualified", "insufficient"]
    reasoning: Text
    qualifications: list[Text] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_qualification(self) -> Self:
        if self.target not in REVIEW_TARGETS:
            raise ValueError("Review only Technical and the 17 commercialization questions.")
        if self.status != "supported" and not self.qualifications:
            raise ValueError("Qualified or insufficient findings need explicit qualifications.")
        return self


class DevelopmentHandoff(Record):
    candidate_architecture: Text | None
    alternatives: list[Text]
    versions_and_licenses: list[Text]
    assumptions: list[Text]
    constraints: list[Text]
    proposed_implementation_scope: list[Text]
    next_validation_tasks: list[Text]
    unresolved_decisions: list[Text]
    limitations: list[Text]
    authority: Literal["proposal_only"] = "proposal_only"

    @model_validator(mode="after")
    def unavailable_architecture(self) -> Self:
        if self.candidate_architecture is None and not self.limitations:
            raise ValueError("An unavailable candidate architecture needs an explicit limitation.")
        return self


class FinalReview(Record):
    items: list[ReviewItem] = Field(min_length=18, max_length=18)
    recommendation: Literal["test_assumption", "change_direction", "stop", "insufficient_evidence"]
    summary: Text
    decision_reasons: list[Text] = Field(min_length=1)
    material_gaps: list[Text]
    contrary_evidence: list[Text]
    handoff: DevelopmentHandoff
    human_review_required: Literal[True] = True

    @model_validator(mode="after")
    def complete_review(self) -> Self:
        if {item.target for item in self.items} != REVIEW_TARGETS:
            raise ValueError("Review every Technical/Market/Business record exactly once.")
        if any(item.status == "insufficient" for item in self.items) and not self.material_gaps:
            raise ValueError("Insufficient evidence must remain visible in the decision's gaps.")
        return self


class AcceptedRecord(Record):
    kind: str
    key: str
    version: int
    content_hash: str
    supersedes_hash: str | None
    payload: dict


class CheckedCalculation(Record):
    target: str
    result: CalculationResult


class Dossier(Record):
    schema_version: Literal["condere.assessment.v1"] = "condere.assessment.v1"
    assessment_id: str
    execution_mode: Literal["simulation", "live"]
    created_at: AwareDatetime
    brief: IntakeBrief
    findings: AssessmentFindings
    review: FinalReview
    record_history: list[AcceptedRecord]
    sources: list[SourceDocument]
    calculations: list[CheckedCalculation]
    spending: dict
    method_version: str
    policy_version: str


def _bullets(values: list[str]) -> str:
    return "\n".join("- " + value for value in values) if values else "None recorded."


def _finding(finding: Finding) -> list[str]:
    lines = [
        finding.conclusion,
        f"Availability: {finding.availability}. Basis: {finding.basis or 'unavailable'}.",
        "",
    ]
    for label, values in (
        ("Assumptions", finding.assumptions),
        ("Limitations", finding.limitations),
        ("Conflicts", finding.conflicts),
        ("Sources", finding.source_ids),
    ):
        if values:
            lines += [f"{label}:", "", _bullets(values), ""]
    return lines


def render_markdown(dossier: Dossier) -> str:
    review = dossier.review
    lines = ["# Condere opportunity assessment", "", review.summary, ""]
    if dossier.execution_mode == "simulation":
        lines += [
            "**Simulation. These records do not establish real research or customer demand.**",
            "",
        ]
    lines += [
        f"Recommendation: **{review.recommendation.replace('_', ' ')}**.",
        "Human review is required. The handoff proposes work; it does not authorize execution.",
        "",
        "## Original intent",
        "",
        dossier.brief.original_input,
        "",
        "## Decision reasons",
        "",
        _bullets(review.decision_reasons),
        "",
        "## Material gaps",
        "",
        _bullets(review.material_gaps),
        "",
        "## Contrary evidence",
        "",
        _bullets(review.contrary_evidence),
        "",
        "## Technical findings",
        "",
    ]
    reviews = {item.target: item for item in review.items}
    for name, finding in dossier.findings.technical.findings.items():
        lines += [f"### {name.replace('_', ' ').title()}", "", *_finding(finding)]
    lines += [
        "Technical review qualifications:",
        "",
        _bullets(reviews["technical"].qualifications),
        "",
        "## Capability records",
        "",
    ]
    for capability in dossier.findings.technical.capabilities:
        lines += [
            f"- {capability.scenario} / {capability.category} / {capability.name}: "
            f"**{capability.status}**. "
            f"{capability.finding.conclusion} Value: {capability.value or 'unavailable'}."
        ]
    if dossier.findings.technical.metrics:
        lines += ["", "## Technical metrics", ""]
        for metric in dossier.findings.technical.metrics:
            lines += [
                f"- {metric.subject} / {metric.scenario} / {metric.name}: "
                f"{metric.value if metric.value is not None else 'unavailable'} {metric.unit} "
                f"({metric.basis}; version {metric.product_version})."
            ]
    lines += ["", "## Commercialization questions", ""]
    questions = {answer.question_id: answer for answer in dossier.findings.questions}
    for identity in QUESTION_IDS:
        answer = questions[identity]
        lines += [f"### {identity}: {QUESTIONS[identity]['question']}", "", *_finding(answer)]
        for name, value in answer.details.items():
            lines += [
                f"- **{name.replace('_', ' ')}:** {value if value is not None else 'Unavailable.'}"
            ]
        lines += [
            "",
            f"Next proposed action: {answer.next_action}",
            "",
            "Review qualifications:",
            "",
            _bullets(reviews[identity].qualifications),
            "",
        ]
    lines += ["## Checked calculations", ""]
    for checked in dossier.calculations:
        result = checked.result
        lines += [
            f"### {checked.target}: {result.calculation.id} ({result.calculation.scenario})",
            "",
            f"Result: {result.value if result.value is not None else 'unavailable'} {result.unit}.",
            "",
        ]
        for item in result.calculation.inputs:
            lines += [
                f"- {item.name}: {item.value if item.value is not None else 'unknown'} "
                f"{item.unit}; "
                f"basis {item.basis or 'unavailable'}; date {item.as_of or 'unavailable'}."
            ]
        if result.limitations:
            lines += ["", _bullets(result.limitations)]
        lines.append("")
    handoff = review.handoff
    lines += [
        "## Development handoff",
        "",
        "Candidate architecture:",
        "",
        handoff.candidate_architecture or "Unavailable; see the handoff limitations.",
        "",
    ]
    for label, values in (
        ("Alternatives", handoff.alternatives),
        ("Versions and licenses", handoff.versions_and_licenses),
        ("Assumptions", handoff.assumptions),
        ("Constraints", handoff.constraints),
        ("Proposed implementation scope", handoff.proposed_implementation_scope),
        ("Next validation tasks", handoff.next_validation_tasks),
        ("Unresolved decisions", handoff.unresolved_decisions),
        ("Limitations", handoff.limitations),
    ):
        lines += [f"### {label}", "", _bullets(values), ""]
    lines += ["## Source index", ""]
    for source in dossier.sources:
        lines += [
            f"- `{source.id}`: {source.origin} (retrieved {source.retrieved_at.isoformat()}). "
            f"[Saved text](sources/{source.id}.txt); [original](sources/{source.id}.original)."
        ]
    money = dossier.spending["budget"]
    lines += [
        "",
        "## Research spending",
        "",
        f"Recorded: ${Decimal(money['settled_microusd']) / 1_000_000}. "
        f"Still reserved or unresolved: ${Decimal(money['pending_microusd']) / 1_000_000}. "
        f"Campaign cap: ${Decimal(money['cap_microusd']) / 1_000_000}.",
        "Reservations are not invoices. The JSON retains cost bases and unresolved liabilities.",
        "",
        f"Assessment: `{dossier.assessment_id}`. Saved: {dossier.created_at.isoformat()}.",
        "",
        "The full JSON retains source passages, comparison conditions, "
        "accepted originals, and every review.",
        "",
    ]
    return "\n".join(lines)


def render_json(dossier: Dossier) -> str:
    return (
        json.dumps(dossier.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )
