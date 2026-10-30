"""The `condere.spinoff.v1` decision contract and its deterministic checks.

The agent submits an `Analysis` (claims, asset, rights, buyer, options, reasoning).
Jev fills a raw `Decision`; `apply_decision_rules` enforces gates and verdict rules
and records every adjustment. Structural validity never proves that evidence
supports a claim; it only keeps references, statuses, and decisions honest.
"""

import math
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, HttpUrl, StringConstraints, model_validator

from condere.runtime.calculations import Record, Text
from condere.runtime.contracts import Citation

SCHEMA = "condere.spinoff.v1"
ClaimId = Annotated[str, StringConstraints(pattern=r"^C\d{1,4}$")]
ComponentId = Annotated[str, StringConstraints(pattern=r"^K\d{1,3}$")]
OptionId = Annotated[str, StringConstraints(pattern=r"^O\d{1,2}$")]
PackageId = Annotated[str, StringConstraints(pattern=r"^W\d{1,3}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ClaimStatus = Literal["observed", "reported", "modelled", "assumed", "unknown"]
EVIDENCED = {"observed", "reported"}
Route = Literal[
    "open_source_owned_ip",
    "open_core_on_existing_oss",
    "managed_service",
    "dual_license",
    "services_only",
    "none",
]
Verdict = Literal["pursue", "reshape", "stop", "insufficient_evidence"]
Gate = Literal["rights_clear", "asset_works"]
Criterion = Literal[
    "buyer_pain",
    "willingness_to_pay",
    "differentiation",
    "build_gap",
    "economics",
    "founder_fit",
    "reversibility",
]
Rating = Literal["strong", "moderate", "weak", "unknown"]
# Below these, Jev's answer is reported as contested rather than escalated to a human.
CONTESTED_TOP_PROBABILITY = 0.5
CONTESTED_MARGIN = 0.15
SUMMARY_LIMIT = 1_500


class Claim(Record):
    id: ClaimId
    statement: Text
    status: ClaimStatus
    citations: list[Citation] = Field(default_factory=list)
    origin_groups: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidenced_status(self) -> Self:
        if self.status in EVIDENCED and not self.citations:
            raise ValueError(f"{self.id} is {self.status} but cites no source passage.")
        if self.status == "unknown" and self.citations:
            raise ValueError(f"{self.id} is unknown; cite it only once it is evidenced.")
        return self


class Repository(Record):
    url: HttpUrl
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] | None = None
    license: Text | None = None


class Component(Record):
    id: ComponentId
    path: Text
    role: Text
    value: Literal["core", "commodity", "glue"]
    claims: list[ClaimId] = Field(default_factory=list)


class Asset(Record):
    summary: Text
    ownership: Literal["owned", "third_party_oss", "mixed", "unknown"]
    repositories: list[Repository] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    hard_to_replicate: list[ClaimId] = Field(default_factory=list)
    maturity: Literal["idea", "prototype", "usable", "production", "unknown"]
    operating_burden: list[ClaimId] = Field(default_factory=list)


class Blocker(Record):
    issue: Text
    component: ComponentId | None = None
    resolve_with: Literal["owner", "counsel", "code_cleanup", "agent"]


class LicenseRecommendation(Record):
    open: Text
    paid: Text
    claims: list[ClaimId] = Field(default_factory=list)


class Rights(Record):
    summary: Text
    can_open_source: Literal["yes", "conditional", "no", "unknown"]
    can_sell_hosted: Literal["yes", "conditional", "no", "unknown"]
    blockers: list[Blocker] = Field(default_factory=list)
    license_recommendation: LicenseRecommendation | None = None


class Buyer(Record):
    summary: Text
    user: Text
    buyer: Text
    trigger: Text
    current_solution: Text
    claims: list[ClaimId] = Field(default_factory=list)


class Option(Record):
    id: OptionId
    route: Route
    open_part: Text
    paid_part: Text
    buyer: Text
    why_pay: Text
    main_risk: Text
    case_for: Text
    case_against: Text
    claims: list[ClaimId] = Field(min_length=1)


class OpenQuestion(Record):
    question: Text
    blocks: Text
    who_can_answer: Literal["agent", "founder", "customer", "counsel"]


class Analysis(Record):
    """Everything the agent establishes before any decision is made."""

    thesis: Text
    summary: Annotated[str, StringConstraints(min_length=1, max_length=SUMMARY_LIMIT)]
    claims: list[Claim] = Field(min_length=1)
    asset: Asset
    rights: Rights
    buyer: Buyer
    options: list[Option] = Field(min_length=2, max_length=4)
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_references(self) -> Self:
        claim_ids = [claim.id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Claim IDs must be distinct.")
        component_ids = [component.id for component in self.asset.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("Component IDs must be distinct.")
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Option IDs must be distinct.")
        referenced = {
            *self.asset.hard_to_replicate,
            *self.asset.operating_burden,
            *self.buyer.claims,
            *(claim for component in self.asset.components for claim in component.claims),
            *(claim for option in self.options for claim in option.claims),
            *(
                self.rights.license_recommendation.claims
                if self.rights.license_recommendation
                else []
            ),
        }
        if missing := sorted(referenced - set(claim_ids)):
            raise ValueError(f"Referenced claims are not in the claims list: {missing}.")
        blocked = {blocker.component for blocker in self.rights.blockers} - {None}
        if missing := sorted(blocked - set(component_ids)):
            raise ValueError(f"Blockers name unknown components: {missing}.")
        return self

    def claim(self, claim_id: str) -> Claim:
        return next(claim for claim in self.claims if claim.id == claim_id)


class WorkPackage(Record):
    id: PackageId
    goal: Text
    touches: list[Text] = Field(default_factory=list)
    depends_on: list[PackageId] = Field(default_factory=list)
    acceptance: Text


class PaidCapability(Record):
    capability: Text
    why: list[ClaimId] = Field(default_factory=list)
    estimate: Text | None = None


class BuildPlan(Record):
    option: OptionId
    keep_open: list[ComponentId] = Field(default_factory=list)
    build_paid: list[PaidCapability] = Field(default_factory=list)
    architecture_delta: list[Text] = Field(default_factory=list)
    work_packages: list[WorkPackage] = Field(default_factory=list)
    not_authorized: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_packages(self) -> Self:
        ids = [package.id for package in self.work_packages]
        if len(ids) != len(set(ids)):
            raise ValueError("Work package IDs must be distinct.")
        for package in self.work_packages:
            if unknown := set(package.depends_on) - set(ids):
                raise ValueError(f"{package.id} depends on unknown packages {sorted(unknown)}.")
        return self


class NextTest(Record):
    assumption: Text
    method: Text
    success: Text
    failure: Text
    cost_and_time: Text


class Distribution(Record):
    value: Text
    distribution: dict[str, Annotated[float, Field(ge=0, le=1)]]
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.value not in self.distribution:
            raise ValueError("A decision value must be one of its distribution's options.")
        if not math.isclose(sum(self.distribution.values()), 1.0, abs_tol=0.02):
            raise ValueError("Decision probabilities must sum to one.")
        return self

    def contested(self) -> bool:
        ranked = sorted(self.distribution.values(), reverse=True)
        margin = ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)
        return ranked[0] < CONTESTED_TOP_PROBABILITY or margin < CONTESTED_MARGIN


class GateResult(Record):
    option: OptionId
    gate: Gate
    p_yes: Annotated[float, Field(ge=0, le=1)]
    result: Literal["pass", "fail", "unknown"]


class RatingResult(Record):
    option: OptionId
    criterion: Criterion
    value: Rating
    distribution: dict[str, Annotated[float, Field(ge=0, le=1)]]
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None


class Adjustment(Record):
    field: Text
    original: Text
    adjusted: Text
    reason: Text


class ArtifactLink(Record):
    ref: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9/_.-]*$")]
    uri: Annotated[str, StringConstraints(pattern=r"^condere://[0-9a-f-]{36}/[a-z0-9/_.-]+$")]
    sha256: Digest
    bytes: int = Field(ge=0)
    media_type: Text
    summary: Text

    @model_validator(mode="after")
    def matching_path(self) -> Self:
        if not self.uri.endswith("/" + self.ref) or ".." in self.ref.split("/"):
            raise ValueError("An artifact URI must name the same relative path as its ref.")
        return self


class Decision(Record):
    verdict: Distribution
    route: Distribution
    option: Distribution
    gates: list[GateResult] = Field(default_factory=list)
    ratings: list[RatingResult] = Field(default_factory=list)
    contested: list[Text] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    model: Text
    brief: ArtifactLink

    @model_validator(mode="after")
    def known_values(self) -> Self:
        if self.verdict.value not in Verdict.__args__:
            raise ValueError("Unknown verdict.")
        if self.route.value not in Route.__args__:
            raise ValueError("Unknown route.")
        return self


def apply_decision_rules(analysis: Analysis, decision: Decision) -> Decision:
    """Enforce gates and verdict rules on Jev's raw answer, recording every change."""
    adjustments = list(decision.adjustments)
    options = {option.id: option for option in analysis.options}
    for name, field, expected in (
        ("verdict", decision.verdict, set(Verdict.__args__)),
        ("route", decision.route, set(Route.__args__)),
        ("option", decision.option, {*options, "none"}),
    ):
        if set(field.distribution) != expected:
            raise ValueError(f"The {name} distribution must cover exactly {sorted(expected)}.")
    gates: dict[str, list[GateResult]] = {}
    for gate in decision.gates:
        if gate.option not in options:
            raise ValueError(f"Gate result names unknown option {gate.option}.")
        gates.setdefault(gate.option, []).append(gate)
    if decision.option.value not in options and decision.option.value != "none":
        raise ValueError("The chosen option is not one of the analysed options.")

    def failed(option_id: str) -> bool:
        return any(gate.result == "fail" for gate in gates.get(option_id, []))

    def unknown(option_id: str) -> bool:
        results = {gate.gate: gate.result for gate in gates.get(option_id, [])}
        return any(results.get(name, "unknown") == "unknown" for name in Gate.__args__)

    def adjust(field: str, target, value: str, reason: str):
        adjustments.append(
            Adjustment(field=field, original=target.value, adjusted=value, reason=reason)
        )
        return target.model_copy(update={"value": value})

    verdict, route, option = decision.verdict, decision.route, decision.option
    if option.value in options and failed(option.value):
        eligible = [
            key
            for key, _ in sorted(option.distribution.items(), key=lambda item: -item[1])
            if key in options and not failed(key)
        ]
        option = adjust(
            "option",
            option,
            eligible[0] if eligible else "none",
            f"{option.value} failed a hard gate; strengths cannot outweigh it.",
        )
    if option.value == "none" and verdict.value == "pursue":
        verdict = adjust("verdict", verdict, "stop", "Every option failed a hard gate.")
    chosen = options.get(option.value)
    if chosen is not None and route.value != chosen.route:
        route = adjust("route", route, chosen.route, "The route follows the chosen option.")
    if verdict.value == "pursue":
        if chosen is None or unknown(chosen.id):
            verdict = adjust(
                "verdict",
                verdict,
                "insufficient_evidence",
                "Pursuing requires every hard gate of the chosen option to be evaluated.",
            )
        elif not any(analysis.claim(claim).status in EVIDENCED for claim in chosen.claims):
            verdict = adjust(
                "verdict",
                verdict,
                "insufficient_evidence",
                "Pursuing requires at least one observed or reported claim behind the option.",
            )
    if verdict.value == "stop":
        weak_pain = all(
            any(
                rating.option == key and rating.criterion == "buyer_pain" and rating.value == "weak"
                for rating in decision.ratings
            )
            for key in options
        )
        if not any(failed(key) for key in options) and not weak_pain:
            verdict = adjust(
                "verdict",
                verdict,
                "reshape",
                "Stopping requires a failed gate or weak buyer pain across every option.",
            )
    contested = list(decision.contested)
    for name, value in (("verdict", verdict), ("route", route), ("option", option)):
        if value.contested() and name not in contested:
            contested.append(name)
    return decision.model_copy(
        update={
            "verdict": verdict,
            "route": route,
            "option": option,
            "adjustments": adjustments,
            "contested": contested,
        }
    )


class Link(Record):
    summary: Text
    full: ArtifactLink


class Counted(Record):
    count: int = Field(ge=0)
    full: ArtifactLink


class Input(Record):
    prompt: Annotated[str, StringConstraints(min_length=1, strip_whitespace=False)]
    repositories: list[HttpUrl] = Field(default_factory=list)
    urls: list[HttpUrl] = Field(default_factory=list)


class ReasoningOption(Record):
    id: OptionId
    case_for: Text
    case_against: Text
    claims: list[ClaimId]


class Reasoning(Record):
    thesis: Text
    summary: Annotated[str, StringConstraints(min_length=1, max_length=SUMMARY_LIMIT)]
    options: list[ReasoningOption] = Field(min_length=2, max_length=4)
    full: ArtifactLink


class AnalysisSummary(Record):
    asset: Link
    rights: Link
    buyer: Link


class SpinoffResult(Record):
    """The compact JSON a coding agent reads first; bulky material is linked."""

    schema_version: Literal["condere.spinoff.v1"] = SCHEMA
    assessment_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f-]{36}$")]
    created_at: AwareDatetime
    input: Input
    decision: Decision
    reasoning: Reasoning
    analysis: AnalysisSummary
    next_test: NextTest
    build_plan: BuildPlan | None
    open_questions: list[OpenQuestion]
    claims: Counted
    sources: Counted
    artifacts: list[ArtifactLink]

    @model_validator(mode="after")
    def linked_artifacts(self) -> Self:
        listed = {(link.ref, link.sha256) for link in self.artifacts}
        named = [
            self.decision.brief,
            self.reasoning.full,
            self.analysis.asset.full,
            self.analysis.rights.full,
            self.analysis.buyer.full,
            self.claims.full,
            self.sources.full,
        ]
        if missing := sorted(link.ref for link in named if (link.ref, link.sha256) not in listed):
            raise ValueError(f"Linked artifacts are missing from the artifact list: {missing}.")
        for link in named:
            if not link.uri.startswith(f"condere://{self.assessment_id}/"):
                raise ValueError("Artifact links must belong to this assessment.")
        if self.build_plan is not None and self.build_plan.option != self.decision.option.value:
            raise ValueError("The build plan must target the chosen option.")
        return self
