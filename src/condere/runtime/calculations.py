"""Reproducible arithmetic with explicit units and unknowns; no model execution."""

import re
from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Basis = Literal["source_observed", "vendor_reported", "measured", "modelled"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CalculationInput(Record):
    name: Text
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    unit: Text
    basis: Basis | None = None
    source_ids: list[Text] = Field(default_factory=list)
    assumptions: list[Text] = Field(default_factory=list)
    limitations: list[Text] = Field(default_factory=list)
    as_of: date | None = None

    @model_validator(mode="after")
    def supported_value(self) -> Self:
        dimensions(self.unit)
        if self.value is None:
            if self.basis is not None or not self.limitations:
                raise ValueError(
                    "An unknown input needs limitations and no claimed evidence basis."
                )
        elif self.basis is None:
            raise ValueError("A numeric input needs an evidence basis.")
        elif self.basis == "modelled":
            if not self.assumptions:
                raise ValueError("A modelled input needs explicit assumptions.")
        elif not self.source_ids:
            raise ValueError("Observed, reported, and measured inputs need source references.")
        if self.value is not None and self.as_of is None:
            raise ValueError("Numeric inputs need their observation, price, or assumption date.")
        return self


class Calculation(Record):
    id: Text
    scenario: Literal["low", "base", "high", "single"]
    operation: Literal["add", "subtract", "multiply", "divide"]
    inputs: list[CalculationInput] = Field(min_length=2, max_length=20)
    output_unit: Text

    @model_validator(mode="after")
    def coherent_inputs(self) -> Self:
        if len({item.name for item in self.inputs}) != len(self.inputs):
            raise ValueError("Calculation input names must be distinct.")
        if self.operation in {"subtract", "divide"} and len(self.inputs) != 2:
            raise ValueError("Subtract and divide require exactly two inputs.")
        dimensions(self.output_unit)
        return self


class CalculationResult(Record):
    calculation: Calculation
    value: Decimal | None = Field(allow_inf_nan=False)
    unit: Text
    unavailable_inputs: list[str]
    limitations: list[str]


def dimensions(unit: str) -> dict[str, int]:
    """Exact symbols, e.g. USD/customer/month; never silently convert currencies or time."""
    result: Counter[str] = Counter()
    sign = 1
    for term in re.split(r"([*/])", unit):
        if term in {"*", "/"}:
            sign = -1 if term == "/" else 1
        elif term == "1":
            continue
        else:
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)(?:\^(-?\d+))?", term)
            if match is None:
                raise ValueError(
                    "Use exact unit symbols joined with * or /, or 1 for dimensionless."
                )
            result[match[1]] += sign * int(match[2] or 1)
    return {key: power for key, power in result.items() if power}


def calculate(calculation: Calculation) -> CalculationResult:
    units = [dimensions(item.unit) for item in calculation.inputs]
    expected = units[0].copy()
    if calculation.operation in {"add", "subtract"}:
        if any(unit != expected for unit in units[1:]):
            raise ValueError("Addition and subtraction require matching units.")
    else:
        for unit in units[1:]:
            for symbol, power in unit.items():
                expected[symbol] = expected.get(symbol, 0) + (
                    -power if calculation.operation == "divide" else power
                )
        expected = {key: power for key, power in expected.items() if power}
    if expected != dimensions(calculation.output_unit):
        raise ValueError("The declared output unit does not match the calculation.")
    missing = [item.name for item in calculation.inputs if item.value is None]
    value = None
    limitations = []
    if missing:
        limitations.append("Unknown inputs prevent a numeric result; none was treated as zero.")
    elif calculation.operation == "divide" and calculation.inputs[1].value == 0:
        limitations.append("Division by zero has no numeric result.")
    else:
        with localcontext() as context:
            context.prec = 34
            value = calculation.inputs[0].value
            for item in calculation.inputs[1:]:
                if calculation.operation == "add":
                    value += item.value
                elif calculation.operation == "subtract":
                    value -= item.value
                elif calculation.operation == "multiply":
                    value *= item.value
                else:
                    value /= item.value
    return CalculationResult(
        calculation=calculation,
        value=value,
        unit=calculation.output_unit,
        unavailable_inputs=missing,
        limitations=limitations,
    )
