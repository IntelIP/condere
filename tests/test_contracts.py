"""Fictional contracts validate coverage and arithmetic, never market research quality."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from condere.runtime.calculations import Calculation, CalculationInput, calculate
from condere.runtime.contracts import (
    QUESTION_IDS,
    QUESTIONS,
    STAGES,
    AssessmentFindings,
    Citation,
    Finding,
    IntakeBrief,
    QuestionAnswer,
)


def test_citations_supply_optional_source_ids_without_accepting_an_explicit_mismatch():
    payload = {
        "conclusion": "Fictional citation normalization check.",
        "availability": "available",
        "basis": "source_observed",
        "citations": [
            {
                "source_id": "b" * 64,
                "text_sha256": "a" * 64,
                "start": 0,
                "end": 5,
                "quote": "hello",
            }
        ],
    }
    assert Finding.model_validate(payload).source_ids == ["b" * 64]
    with pytest.raises(ValidationError, match="Listed without a citation"):
        Finding.model_validate(payload | {"source_ids": []})


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
    unavailable = unavailable or "modelled" not in QUESTIONS[question_id]["allowed_bases"]
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


def test_technical_questions_cover_the_open_core_hosting_thesis():
    assert STAGES["technical"] == tuple(f"T{index}" for index in range(1, 10))
    assert [QUESTIONS[key]["stage"] for key in QUESTION_IDS[:9]] == ["technical"] * 9
    assert {"hosting_value"} <= set(QUESTIONS["T5"]["required_fields"])
    assert {"hosting_rights"} <= set(QUESTIONS["T6"]["required_fields"])
    assert {"open_core_boundary"} <= set(QUESTIONS["T9"]["required_fields"])
    for key in STAGES["technical"]:
        assert QUESTIONS[key]["skills"][1:] == ["repository-index", "code-exploration"]
    # Architecture and applications can be proposed for an idea with no repository.
    assert (
        not QUESTIONS["T1"]["allow_unavailable"] and "modelled" in QUESTIONS["T1"]["allowed_bases"]
    )
    assert "modelled" not in QUESTIONS["T2"]["allowed_bases"]


def test_full_coverage_preserves_allowed_unknowns_and_rejects_missing_questions():
    answers = [answer(question_id, unavailable=question_id == "M3") for question_id in QUESTION_IDS]
    findings = AssessmentFindings(questions=answers)
    assert len(findings.questions) == 26
    assert findings.questions[QUESTION_IDS.index("M3")].availability == "unavailable"
    with pytest.raises(ValidationError):
        AssessmentFindings(questions=answers[:-1])
    with pytest.raises(ValidationError, match="once, in order"):
        AssessmentFindings(questions=answers[:-1] + [answers[0]])
    with pytest.raises(ValidationError, match="does not permit"):
        answer("M1", unavailable=True)
    with pytest.raises(ValidationError, match="does not permit"):
        answer("T1", unavailable=True)
    broken = answer("M6").model_dump()
    del broken["details"]["billing_conditions"]
    with pytest.raises(ValidationError, match=r"Missing: \[.billing_conditions.\]"):
        QuestionAnswer.model_validate(broken)
    incomplete = answer("B3").model_dump()
    incomplete["calculations"] = incomplete["calculations"][:1]
    with pytest.raises(ValidationError, match="low/base/high"):
        QuestionAnswer.model_validate(incomplete)


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


def test_citations_need_only_a_source_and_exact_quote():
    quote = "  Exact source whitespace.\n"
    citation = Citation(source_id="example", quote=quote)
    assert citation.quote == quote and citation.text_sha256 is None
    with pytest.raises(ValidationError, match="bounded source window"):
        Citation(source_id="example", quote="x", start=5, end=5)
    with pytest.raises(ValidationError):
        Citation(source_id="example", quote="")
