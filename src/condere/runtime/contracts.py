"""Application-enforced coverage using Condere's preserved question contracts.

These checks establish structure, references, and honest labels. A final reviewer
must still judge whether the cited evidence supports each conclusion.
"""

import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, StringConstraints, model_validator

from condere.runtime.calculations import Basis, Calculation, Record, Text, calculate

METHODS = Path(__file__).resolve().parents[3] / "agent/references/questions.json"
QUESTIONS = json.loads(METHODS.read_text())
TECHNICAL_IDS = tuple(f"T{index}" for index in range(1, 10))
MARKET_IDS = tuple(f"M{index}" for index in range(1, 9))
BUSINESS_IDS = tuple(f"B{index}" for index in range(1, 10))
QUESTION_IDS = TECHNICAL_IDS + MARKET_IDS + BUSINESS_IDS
if set(QUESTIONS) != set(QUESTION_IDS):
    raise ValueError("The preserved contract must contain all T1-T9, M1-M8 and B1-B9 questions.")
STAGES = {"technical": TECHNICAL_IDS, "market": MARKET_IDS, "business": BUSINESS_IDS}


class Citation(Record):
    """Cite by source ID and exact quoted text; the application resolves hash and offsets."""

    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    source_id: Text
    quote: Annotated[str, Field(min_length=1, max_length=12_000)]
    text_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bounded_window(self) -> Self:
        if self.start is not None and self.end is not None:
            if self.end <= self.start or self.end - self.start > 12_000:
                raise ValueError("A citation must name a nonempty, bounded source window.")
        return self


class Finding(Record):
    conclusion: Text
    availability: Literal["available", "unavailable"]
    basis: Basis | None = None
    source_ids: list[Text] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    assumptions: list[Text] = Field(default_factory=list)
    limitations: list[Text] = Field(default_factory=list)
    conflicts: list[Text] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def citation_source_ids(cls, value):
        if (
            isinstance(value, dict)
            and "source_ids" not in value
            and isinstance(value.get("citations"), list)
        ):
            identities = [
                item.source_id if isinstance(item, Citation) else item.get("source_id")
                for item in value["citations"]
                if isinstance(item, (dict, Citation))
            ]
            return value | {
                "source_ids": list(
                    dict.fromkeys(item for item in identities if isinstance(item, str))
                )
            }
        return value

    @model_validator(mode="after")
    def honest_basis(self) -> Self:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Source references must be distinct.")
        cited = {citation.source_id for citation in self.citations}
        if cited != set(self.source_ids):
            raise ValueError(
                "Every listed source needs a citation and every citation a listed source. "
                f"Listed without a citation: {sorted(set(self.source_ids) - cited)}. "
                f"Cited but not listed: {sorted(cited - set(self.source_ids))}. "
                "Omit source_ids to derive it from the citations."
            )
        if self.availability == "unavailable":
            if self.basis is not None or not self.limitations:
                problems = ([f"basis must be null (got {self.basis!r})"] if self.basis else []) + (
                    ["limitations must name why the evidence is unavailable"]
                    if not self.limitations
                    else []
                )
                raise ValueError("Unavailable findings: " + "; ".join(problems) + ".")
        elif self.basis is None:
            raise ValueError("An available finding needs an evidence basis.")
        elif self.basis == "modelled":
            if not self.assumptions:
                raise ValueError("Modelled findings must state their assumptions.")
        elif not self.source_ids:
            raise ValueError("Observed, vendor-reported, and measured findings need sources.")
        return self


class QuestionAnswer(Finding):
    question_id: Text
    details: dict[str, Text | None]
    calculations: list[Calculation] = Field(default_factory=list)
    next_action: Text

    @model_validator(mode="after")
    def required_contract(self) -> Self:
        contract = QUESTIONS.get(self.question_id)
        if contract is None:
            raise ValueError("Unknown commercialization question.")
        if set(self.details) != set(contract["required_fields"]):
            required, given = set(contract["required_fields"]), set(self.details)
            raise ValueError(
                f"{self.question_id} details must have exactly these keys: "
                f"{sorted(required)}. Missing: {sorted(required - given)}. "
                f"Not allowed: {sorted(given - required)}."
            )
        if self.availability == "unavailable":
            if not contract["allow_unavailable"]:
                raise ValueError(f"{self.question_id} does not permit an unavailable answer.")
        else:
            if self.basis not in contract["allowed_bases"] or any(
                value is None for value in self.details.values()
            ):
                raise ValueError(
                    "Available answers need an allowed basis and nonempty required fields."
                )
            if self.question_id == "B3" and not {
                "low",
                "base",
                "high",
            }.issubset(item.scenario for item in self.calculations):
                raise ValueError(
                    "Available economics must include reproducible low/base/high scenarios."
                )
        if len({item.id for item in self.calculations}) != len(self.calculations):
            raise ValueError("Calculation identities must be distinct.")
        for item in self.calculations:
            calculate(item)
        return self


class RepositoryInspection(Record):
    repository_url: Text
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    archive_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    archive_source_id: Text
    machine_id: Text
    receipt_id: Text
    network_enabled: Literal[False]
    inspected_files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    limitations: list[Text] = Field(default_factory=list)


class AssessmentFindings(Record):
    questions: list[QuestionAnswer] = Field(min_length=26, max_length=26)

    @model_validator(mode="after")
    def all_questions(self) -> Self:
        if [item.question_id for item in self.questions] != list(QUESTION_IDS):
            raise ValueError("An assessment requires T1-T9, M1-M8 and B1-B9 once, in order.")
        return self


class ScreeningCandidate(Record):
    name: Text
    customer: Text
    problem: Finding
    commercial_evidence: Finding
    alternatives: Finding


class NextCheck(Record):
    question: Text
    action: Text
    execution: Literal["agent_research", "customer_input", "human_action"]
    decision_impact: Text


class OpportunityScreen(Record):
    recommendation: Literal["deepen", "reframe", "stop", "needs_customer_evidence"]
    summary: Text
    candidates: list[ScreeningCandidate] = Field(min_length=1, max_length=4)
    selected_candidate: Text | None = None
    decision_reasons: list[Text] = Field(min_length=1)
    material_gaps: list[Text]
    contrary_evidence: list[Finding] = Field(default_factory=list)
    next_checks: list[NextCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def executable_deeper_research(self) -> Self:
        names = [candidate.name for candidate in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError("Candidate names must be distinct.")
        if self.selected_candidate is not None and self.selected_candidate not in names:
            raise ValueError("Select a candidate from the screened alternatives.")
        if self.recommendation == "deepen" and (
            self.selected_candidate is None
            or not any(check.execution == "agent_research" for check in self.next_checks)
        ):
            raise ValueError("Deepening needs a selected candidate and executable research.")
        return self


class IntakeBrief(Record):
    analysis_depth: Literal["adaptive", "deep"] = "adaptive"
    original_input: Annotated[Text, StringConstraints(strip_whitespace=False)]
    supplied_facts: list[Text] = Field(default_factory=list)
    technology: Text | None = None
    idea: Text | None = None
    public_references: list[HttpUrl] = Field(default_factory=list)
    source_ids: list[Text] = Field(default_factory=list)
    customer_hypotheses: list[Text] = Field(default_factory=list)
    founder_resources: list[Text] = Field(default_factory=list)
    constraints: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_reference_credentials(self) -> Self:
        if not self.original_input.strip():
            raise ValueError("The original input cannot be blank.")
        if any(url.username or url.password for url in self.public_references):
            raise ValueError("Public references cannot include credentials.")
        return self
