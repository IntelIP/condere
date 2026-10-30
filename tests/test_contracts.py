"""Fictional contracts validate intake, citations and arithmetic, never research quality."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from condere.runtime.calculations import Calculation, CalculationInput, calculate
from condere.runtime.contracts import Citation, IntakeBrief


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
