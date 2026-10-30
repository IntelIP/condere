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
    OpportunityScreen,
)
from condere.runtime.evidence import SourceDocument

REVIEW_TARGETS = set(QUESTION_IDS)


class ReviewItem(Record):
    target: Text
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["supported", "qualified", "insufficient"]
    reasoning: Text
    qualifications: list[Text] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def plain_question_target(cls, value):
        # read_assessment keys accepted records as "question:T1"; reviewers reasonably reuse it.
        if isinstance(value, dict) and str(value.get("target", "")).startswith("question:"):
            return value | {"target": value["target"].removeprefix("question:")}
        return value

    @model_validator(mode="after")
    def explicit_qualification(self) -> Self:
        if self.target not in REVIEW_TARGETS:
            raise ValueError(
                f"Unknown review target {self.target!r}; use a question ID: "
                + ", ".join(QUESTION_IDS)
                + "."
            )
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
    items: list[ReviewItem] = Field(min_length=26, max_length=26)
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


class ReportAsset(Record):
    path: str = Field(pattern=r"^figures/[a-z0-9-]+\.(png|svg|json|csv)$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    media_type: Literal["image/png", "image/svg+xml", "application/json", "text/csv"]


class ReportFigure(Record):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    caption: str
    source_ids: list[str]
    paths: dict[str, str]


class Dossier(Record):
    schema_version: Literal["condere.assessment.v2"] = "condere.assessment.v2"
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
    screening: OpportunityScreen | None = None
    figures: list[ReportFigure] = Field(default_factory=list)
    assets: list[ReportAsset] = Field(default_factory=list)


class ScreeningResult(Record):
    schema_version: Literal["condere.screen.v1"] = "condere.screen.v1"
    assessment_id: str
    execution_mode: Literal["simulation", "live"]
    created_at: AwareDatetime
    brief: IntakeBrief
    screen: OpportunityScreen
    sources: list[SourceDocument]
    spending: dict
    method_version: str
    policy_version: str
    deep_assessment_completed: Literal[False] = False
    human_review_required: Literal[True] = True
    assets: list[ReportAsset] = Field(default_factory=list)


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


def render_markdown(dossier: Dossier | ScreeningResult) -> str:
    if isinstance(dossier, ScreeningResult):
        screen = dossier.screen
        lines = [
            "# Condere opportunity screen",
            "",
            screen.summary,
            "",
            f"Recommendation: **{screen.recommendation.replace('_', ' ')}**.",
            "Qualification is complete. A full Technical/Market/Business assessment has not run.",
            "Proposed customer actions and implementation require human review.",
            "",
            "## Decision reasons",
            "",
            _bullets(screen.decision_reasons),
            "",
            "## What can be done next",
            "",
        ]
        if dossier.execution_mode == "simulation":
            lines.insert(2, "**Simulation. These records do not establish customer demand.**\n")
        for check in screen.next_checks:
            lines += [
                f"### {check.question}",
                "",
                f"Execution: **{check.execution.replace('_', ' ')}**.",
                "",
                check.action,
                "",
                f"Decision impact: {check.decision_impact}",
                "",
            ]
        lines += ["## Material gaps", "", _bullets(screen.material_gaps), ""]
        for candidate in screen.candidates:
            lines += [f"## {candidate.name}", "", f"Possible customer: {candidate.customer}", ""]
            for name in ("problem", "commercial_evidence", "alternatives"):
                lines += [
                    f"### {name.replace('_', ' ').title()}",
                    "",
                    *_finding(getattr(candidate, name)),
                ]
        lines += ["## Contrary evidence", ""]
        for finding in screen.contrary_evidence:
            lines += _finding(finding)
        lines += ["## Saved sources", ""]
        lines += [
            f"- [{source.origin}]({source.origin}) — `{source.id}`." for source in dossier.sources
        ]
        lines += ["", "## Recorded usage", "", json.dumps(dossier.spending, sort_keys=True), ""]
        return "\n".join(lines)
    review = dossier.review
    figures = []
    for figure in dossier.figures:
        figures += [
            f"### {figure.title}",
            "",
            f"![{figure.title}]({figure.paths['png']})",
            "",
            figure.caption,
            "",
            f"[Figure data]({figure.paths['data']}) · [Scalable figure]({figure.paths['svg']})",
            "",
        ]
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
        *figures,
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
    ]
    reviews = {item.target: item for item in review.items}
    questions = {answer.question_id: answer for answer in dossier.findings.questions}
    stage = None
    for identity in QUESTION_IDS:
        answer = questions[identity]
        if QUESTIONS[identity]["stage"] != stage:
            stage = QUESTIONS[identity]["stage"]
            lines += [f"## {stage.title()} questions", ""]
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
        "Campaign cap: "
        + (
            "none; every charge is recorded."
            if money["cap_microusd"] is None
            else f"${Decimal(money['cap_microusd']) / 1_000_000}."
        ),
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
