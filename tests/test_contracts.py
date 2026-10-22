"""Fictional contracts validate coverage and arithmetic, never market research quality."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from condere.runtime.calculations import Calculation, CalculationInput, calculate
from condere.runtime.contracts import (
    CAPABILITY_CATEGORIES,
    QUESTION_IDS,
    QUESTIONS,
    TECHNICAL_FIELDS,
    AssessmentFindings,
    Citation,
    IntakeBrief,
    Metric,
    QuestionAnswer,
    TechnicalReport,
)


def assumed(name, value, unit):
    return CalculationInput(
        name=name,
        value=Decimal(value),
        unit=unit,
        basis="modelled",
        assumptions=["Fictional fixture, not observed economics."],
        as_of="2026-10-21",
    )


def economics(scenario="base"):
    return Calculation(
        id="fixture-revenue-" + scenario,
        scenario=scenario,
        operation="multiply",
        inputs=[
            assumed("customers", "10", "customer"),
            assumed("price", "20", "USD/customer/month"),
        ],
        output_unit="USD/month",
    )


def answer(question_id, *, unavailable=False):
    return QuestionAnswer(
        question_id=question_id,
        conclusion="Fictional hypothesis for contract checks only.",
        availability="unavailable" if unavailable else "available",
        basis=None if unavailable else "modelled",
        details={
            key: None if unavailable else "Explicitly hypothetical fixture detail."
            for key in QUESTIONS[question_id]["required_fields"]
        },
        assumptions=[]
        if unavailable
        else ["No real customers, demand, or measurements are represented."],
        limitations=["This fixture has no live research."],
        calculations=[economics(scenario) for scenario in ("low", "base", "high")]
        if question_id == "B3" and not unavailable
        else [],
        next_action="Human review of missing evidence; do not contact or build.",
    )


def technical():
    missing = dict(
        conclusion="Not established by this fixture.",
        availability="unavailable",
        limitations=["No native research was run."],
    )
    return TechnicalReport(
        findings={field: missing for field in TECHNICAL_FIELDS},
        capabilities=[
            dict(
                scenario="fixture",
                category=category,
                name="required-" + category,
                status="unavailable",
                value=None,
                finding=missing,
            )
            for category in sorted(CAPABILITY_CATEGORIES)
        ],
        inspection_limitations=["This fixture does not inspect a repository."],
    )


@pytest.mark.parametrize(
    "brief",
    [
        {
            "original_input": "What software opportunity could this technology support?",
            "technology": "Fictional library",
        },
        {"original_input": "Assess my existing idea.", "idea": "Fictional document tool"},
        {"original_input": "  I have no evidence that customers want this.\n"},
    ],
)
def test_intake_preserves_intent_without_requiring_customer_assumptions(brief):
    accepted = IntakeBrief.model_validate(brief)
    assert accepted.original_input == brief["original_input"]
    assert accepted.customer_hypotheses == []
    assert accepted.supplied_facts == []
    with pytest.raises(ValidationError):
        IntakeBrief.model_validate(brief | {"budget_usd": 100})
    with pytest.raises(ValidationError):
        IntakeBrief.model_validate(brief | {"owner_id": "someone-else"})


def test_full_coverage_preserves_allowed_unknowns_and_rejects_missing_questions():
    answers = [answer(question_id, unavailable=question_id == "M3") for question_id in QUESTION_IDS]
    findings = AssessmentFindings(technical=technical(), questions=answers)
    assert len(findings.questions) == 17
    assert findings.questions[2].availability == "unavailable"
    with pytest.raises(ValidationError):
        AssessmentFindings(technical=technical(), questions=answers[:-1])
    with pytest.raises(ValidationError, match="exactly once"):
        AssessmentFindings(technical=technical(), questions=answers[:-1] + [answers[0]])
    with pytest.raises(ValidationError, match="does not permit"):
        answer("M1", unavailable=True)
    broken = answer("M6").model_dump()
    del broken["details"]["billing_conditions"]
    with pytest.raises(ValidationError, match="required field"):
        QuestionAnswer.model_validate(broken)
    incomplete = answer("B3").model_dump()
    incomplete["calculations"] = incomplete["calculations"][:1]
    with pytest.raises(ValidationError, match="low/base/high"):
        QuestionAnswer.model_validate(incomplete)
    missing_topic = technical().model_dump()
    del missing_topic["findings"]["licensing"]
    with pytest.raises(ValidationError, match="required topic"):
        TechnicalReport.model_validate(missing_topic)


def test_calculations_preserve_units_dates_assumptions_and_unknown_costs():
    result = calculate(economics())
    assert result.value == Decimal("200")
    assert result.unit == "USD/month"
    assert result.calculation.inputs[1].as_of.isoformat() == "2026-10-21"
    unknown_cost = CalculationInput(
        name="support",
        value=None,
        unit="USD/month",
        limitations=["Support workload has not been measured."],
    )
    contribution = Calculation(
        id="fixture-contribution",
        scenario="base",
        operation="subtract",
        inputs=[assumed("revenue", "200", "USD/month"), unknown_cost],
        output_unit="USD/month",
    )
    assert calculate(contribution).value is None
    assert calculate(contribution).unavailable_inputs == ["support"]
    wrong_units = economics().model_copy(update={"output_unit": "USD"})
    with pytest.raises(ValueError, match="output unit"):
        calculate(wrong_units)
    with pytest.raises(ValidationError, match="date"):
        CalculationInput(
            name="price",
            value="20",
            unit="EUR/customer/month",
            basis="modelled",
            assumptions=["Hypothesis"],
        )
    with pytest.raises(ValidationError):
        assumed("not-finite", "NaN", "USD")
    divide = Calculation(
        id="zero",
        scenario="single",
        operation="divide",
        inputs=[assumed("cost", "5", "USD"), assumed("customers", "0", "customer")],
        output_unit="USD/customer",
    )
    assert calculate(divide).value is None
    assert "Division by zero" in calculate(divide).limitations[0]


def test_claims_cannot_become_measurements_without_sources_and_receipts():
    missing = dict(
        scenario="fixture",
        subject="example",
        name="p95_latency",
        value="1.2",
        unit="second",
        basis="measured",
        product_version="1.0",
    )
    with pytest.raises(ValidationError, match="protected receipts"):
        Metric.model_validate(missing)
    with pytest.raises(ValidationError, match="null values"):
        Metric.model_validate(missing | {"basis": "unavailable", "limitations": ["Not measured"]})
    with pytest.raises(ValidationError, match="match its checked calculation"):
        Metric(
            scenario="fixture",
            subject="example",
            name="delivery_cost",
            value="999",
            unit="USD/month",
            basis="modelled",
            product_version="1.0",
            calculation=economics(),
            assumptions=["Fixture"],
        )
    quote = "  Exact source whitespace.\n"
    citation = Citation(
        source_id="example", text_sha256="a" * 64, start=0, end=len(quote), quote=quote
    )
    assert citation.quote == quote
