"""Application-enforced coverage using Condere's preserved question contracts.

These checks establish structure, references, and honest labels. A final reviewer
must still judge whether the cited evidence supports each conclusion.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, StringConstraints, model_validator

from condere.runtime.calculations import Basis, Calculation, Record, Text, calculate

METHODS = Path(__file__).resolve().parents[3] / "agent/references/questions.json"
QUESTIONS = json.loads(METHODS.read_text())
QUESTION_IDS = tuple(
    [f"M{index}" for index in range(1, 9)] + [f"B{index}" for index in range(1, 10)]
)
if set(QUESTIONS) != set(QUESTION_IDS):
    raise ValueError("The preserved contract must contain all M1-M8 and B1-B9 questions.")

TECHNICAL_FIELDS = (
    "capabilities",
    "use_cases",
    "maturity",
    "versions",
    "licensing",
    "limitations",
    "integration",
    "hosting_modes",
    "regions",
    "hardware",
    "operating_burden",
    "costs",
    "alternatives",
)
CAPABILITY_CATEGORIES = {"use_case", "hosting_mode", "region", "hardware", "operating_burden"}


class Citation(Record):
    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    source_id: Text
    text_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: Annotated[str, Field(min_length=1, max_length=12_000)]

    @model_validator(mode="after")
    def bounded_window(self) -> Self:
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

    @model_validator(mode="after")
    def honest_basis(self) -> Self:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Source references must be distinct.")
        if {citation.source_id for citation in self.citations} != set(self.source_ids):
            raise ValueError(
                "Every cited source needs a retrievable passage, with no unlisted sources."
            )
        if self.availability == "unavailable":
            if self.basis is not None or not self.limitations:
                raise ValueError(
                    "Unavailable findings need a limitation and no claimed evidence basis."
                )
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
            raise ValueError(f"{self.question_id} must include each required field exactly once.")
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


class Capability(Record):
    scenario: Text
    category: Literal["use_case", "hosting_mode", "region", "hardware", "operating_burden"]
    name: Text
    status: Literal["supported", "unsupported", "unavailable"]
    value: Text | None = None
    finding: Finding

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if self.status == "unavailable":
            if self.value is not None or self.finding.availability != "unavailable":
                raise ValueError(
                    "Unknown capabilities retain a null value and an unavailable finding."
                )
        elif self.finding.availability != "available" or not self.finding.source_ids:
            raise ValueError(
                "Supported and unsupported capabilities require attributable evidence."
            )
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


class ComparisonConditions(Record):
    workload: Text
    fixture_id: Text
    measurement_protocol_id: Text
    baseline_id: Text
    region: Text


class Metric(Record):
    scenario: Text
    subject: Text
    name: Literal["customer_price", "delivery_cost", "p95_latency", "throughput"]
    value: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: Text
    basis: Literal["measured", "vendor_reported", "modelled", "unavailable"]
    source_ids: list[Text] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    receipt_id: Text | None = None
    product_version: Text
    conditions: ComparisonConditions | None = None
    calculation: Calculation | None = None
    assumptions: list[Text] = Field(default_factory=list)
    limitations: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def supported_metric(self) -> Self:
        if {citation.source_id for citation in self.citations} != set(self.source_ids):
            raise ValueError("Metric sources need retrievable passages.")
        if self.basis == "unavailable":
            if self.value is not None or not self.limitations:
                raise ValueError("Unavailable metrics need null values and explicit limitations.")
        elif self.value is None:
            raise ValueError("A numeric metric cannot omit its value.")
        elif self.basis == "measured":
            if not self.receipt_id or self.conditions is None or not self.source_ids:
                raise ValueError(
                    "Measurements need protected receipts, sources, and comparison conditions."
                )
        elif self.basis == "vendor_reported" and not self.source_ids:
            raise ValueError("Vendor-reported metrics need attributable sources.")
        elif self.basis == "modelled":
            if not self.assumptions or self.calculation is None:
                raise ValueError(
                    "Modelled metrics need explicit assumptions and reproducible arithmetic."
                )
            result = calculate(self.calculation)
            if result.value != self.value or result.unit != self.unit:
                raise ValueError("The modelled metric must match its checked calculation.")
        return self


class TechnicalReport(Record):
    findings: dict[str, Finding]
    capabilities: list[Capability] = Field(min_length=5)
    repositories: list[RepositoryInspection] = Field(default_factory=list, max_length=3)
    metrics: list[Metric] = Field(default_factory=list)
    inspection_limitations: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_technical_coverage(self) -> Self:
        if set(self.findings) != set(TECHNICAL_FIELDS):
            raise ValueError("Technical analysis must cover every required topic.")
        keys = [(item.scenario, item.category, item.name) for item in self.capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError("Required capability records must be distinct.")
        if {item.category for item in self.capabilities} != CAPABILITY_CATEGORIES:
            raise ValueError(
                "Technical capabilities must cover use, hosting, region, hardware, and operations."
            )
        if not self.repositories and not self.inspection_limitations:
            raise ValueError("Explain why repository inspection is unavailable or not applicable.")
        if len({item.repository_url for item in self.repositories}) != len(self.repositories):
            raise ValueError("Inspect each admitted repository at one exact commit.")
        metric_keys = [(item.scenario, item.subject, item.name) for item in self.metrics]
        if len(set(metric_keys)) != len(metric_keys):
            raise ValueError("Return each scenario/subject/metric once.")
        return self


class AssessmentFindings(Record):
    technical: TechnicalReport
    questions: list[QuestionAnswer] = Field(min_length=17, max_length=17)

    @model_validator(mode="after")
    def all_questions(self) -> Self:
        if {item.question_id for item in self.questions} != set(QUESTION_IDS):
            raise ValueError("An assessment requires all M1-M8 and B1-B9 exactly once.")
        return self


class IntakeBrief(Record):
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
