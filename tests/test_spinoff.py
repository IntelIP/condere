"""Fictional spin-off records check references and decision rules, never research quality."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from condere.runtime.spinoff import (
    Analysis,
    ArtifactLink,
    Decision,
    Route,
    SpinoffResult,
    Verdict,
    apply_decision_rules,
)

ASSESSMENT = "00000000-0000-4000-8000-000000000108"
SOURCE = "b" * 64
NEXT_TEST = {
    "assumption": "Teams pay to delegate upgrades.",
    "method": "Priced interviews",
    "success": "Three priced commitments",
    "failure": "No priced interest",
    "cost_and_time": "Two weeks",
}


def analysis(**changes) -> dict:
    payload = {
        "thesis": "Keep the engine open; sell managed lifecycle operations to app teams.",
        "summary": "Fictional summary for contract checks.",
        "claims": [
            {
                "id": "C1",
                "statement": "Operators report recurring upgrade pain.",
                "status": "observed",
                "citations": [{"source_id": SOURCE, "quote": "upgrades keep breaking"}],
            },
            {"id": "C2", "statement": "Buyers would pay per database.", "status": "modelled"},
            {"id": "C3", "statement": "Retention is unmeasured.", "status": "unknown"},
        ],
        "asset": {
            "summary": "An embeddable vector extension.",
            "ownership": "third_party_oss",
            "components": [{"id": "K1", "path": "src/", "role": "engine", "value": "core"}],
            "maturity": "usable",
            "operating_burden": ["C1"],
        },
        "rights": {
            "summary": "Permissive license.",
            "can_open_source": "yes",
            "can_sell_hosted": "yes",
        },
        "buyer": {
            "summary": "App teams running server-side databases.",
            "user": "Backend engineer",
            "buyer": "Engineering lead",
            "trigger": "A failed upgrade",
            "current_solution": "Manual maintenance",
            "claims": ["C1"],
        },
        "options": [
            option("O1", "managed_service", ["C1", "C2"]),
            option("O2", "services_only", ["C2"]),
        ],
        "next_test": NEXT_TEST,
    }
    return payload | changes


def option(identifier: str, route: str, claims: list[str]) -> dict:
    return {
        "id": identifier,
        "route": route,
        "open_part": "The engine",
        "paid_part": "Operations",
        "buyer": "Engineering lead",
        "why_pay": "Avoid upgrade incidents",
        "main_risk": "Self-hosting is cheap",
        "case_for": "Observed pain",
        "case_against": "Unmeasured willingness to pay",
        "claims": claims,
    }


def link(ref: str) -> dict:
    return {
        "ref": ref,
        "uri": f"condere://{ASSESSMENT}/{ref}",
        "sha256": "c" * 64,
        "bytes": 10,
        "media_type": "text/markdown",
        "summary": f"Fictional {ref}",
    }


def spread(options: list[str], chosen: str, top: float = 0.7) -> dict:
    rest = (1 - top) / (len(options) - 1)
    return {
        "value": chosen,
        "distribution": {key: top if key == chosen else rest for key in options},
        "confidence": 0.6,
    }


def decision(verdict="pursue", chosen="O1", route="managed_service", gates=None, ratings=None):
    passed = [
        {"option": key, "gate": gate, "p_yes": 0.9, "result": "pass"}
        for key in ("O1", "O2")
        for gate in ("rights_clear", "asset_works")
    ]
    return Decision.model_validate(
        {
            "verdict": spread(list(Verdict.__args__), verdict),
            "route": spread(list(Route.__args__), route),
            "option": spread(["O1", "O2", "none"], chosen),
            "gates": passed if gates is None else gates,
            "ratings": ratings or [],
            "model": "typesafe/jev-1.14",
            "brief": link("artifacts/decision-brief.md"),
        }
    )


def test_analysis_requires_evidenced_claims_and_known_references():
    Analysis.model_validate(analysis())
    unsupported = analysis()
    unsupported["claims"][0]["citations"] = []
    with pytest.raises(ValidationError, match="observed but cites no source passage"):
        Analysis.model_validate(unsupported)
    with pytest.raises(ValidationError, match="not in the claims list"):
        Analysis.model_validate(
            analysis(options=[option("O1", "none", ["C9"]), option("O2", "none", ["C1"])])
        )
    with pytest.raises(ValidationError, match="Option IDs must be distinct"):
        Analysis.model_validate(analysis(options=[option("O1", "none", ["C1"])] * 2))
    cited_unknown = analysis()
    cited_unknown["claims"][2]["citations"] = [{"source_id": SOURCE, "quote": "x"}]
    with pytest.raises(ValidationError, match="unknown; cite it only once"):
        Analysis.model_validate(cited_unknown)


def test_a_failed_gate_moves_the_choice_and_records_the_adjustment():
    gates = [
        {"option": "O1", "gate": "rights_clear", "p_yes": 0.1, "result": "fail"},
        {"option": "O1", "gate": "asset_works", "p_yes": 0.9, "result": "pass"},
        {"option": "O2", "gate": "rights_clear", "p_yes": 0.9, "result": "pass"},
        {"option": "O2", "gate": "asset_works", "p_yes": 0.9, "result": "pass"},
    ]
    result = apply_decision_rules(Analysis.model_validate(analysis()), decision(gates=gates))
    assert result.option.value == "O2"
    assert result.route.value == "services_only"
    # O2 rests only on a modelled claim, so the guidance is not to pursue it.
    assert result.verdict.value == "stop"
    assert [item.field for item in result.adjustments] == ["option", "route", "verdict"]


def test_pursue_needs_evaluated_gates_and_evidenced_claims():
    unevaluated = apply_decision_rules(Analysis.model_validate(analysis()), decision(gates=[]))
    # Guidance is pursue or stop; an unresolved gate becomes part of what the next test settles.
    assert unevaluated.verdict.value == "pursue"
    assert any("unresolved hard gate" in item for item in unevaluated.contested)
    supported = apply_decision_rules(Analysis.model_validate(analysis()), decision())
    assert supported.verdict.value == "pursue"
    assert supported.adjustments == []


def test_guidance_is_pursue_or_stop_and_old_results_stay_readable():
    stopped = apply_decision_rules(Analysis.model_validate(analysis()), decision(verdict="stop"))
    assert stopped.verdict.value == "stop" and stopped.adjustments == []
    no_option = apply_decision_rules(Analysis.model_validate(analysis()), decision(chosen="none"))
    assert no_option.verdict.value == "stop"
    retired = decision().model_dump(mode="json")
    retired["verdict"] = {
        "value": "reshape",
        "distribution": {"pursue": 0.2, "reshape": 0.5, "stop": 0.1, "insufficient_evidence": 0.2},
        "confidence": 0.6,
    }
    assert Decision.model_validate(retired).verdict.value == "reshape"  # Published before.


def test_even_answers_are_reported_as_contested():
    raw = decision()
    even = raw.model_copy(
        update={"option": raw.option.model_validate(spread(["O1", "O2", "none"], "O1", 0.45))}
    )
    assert "option" in apply_decision_rules(Analysis.model_validate(analysis()), even).contested


def test_distributions_must_cover_every_choice():
    raw = decision()
    partial = raw.model_copy(
        update={"option": raw.option.model_validate(spread(["O1", "none"], "O1"))}
    )
    with pytest.raises(ValueError, match="option distribution must cover"):
        apply_decision_rules(Analysis.model_validate(analysis()), partial)


def test_artifact_links_must_name_the_same_path():
    with pytest.raises(ValidationError, match="same relative path"):
        ArtifactLink.model_validate(link("artifacts/claims.json") | {"ref": "artifacts/other.json"})


def result_payload() -> dict:
    refs = {
        "brief": "artifacts/decision-brief.md",
        "reasoning": "artifacts/reasoning.md",
        "asset": "artifacts/asset.md",
        "rights": "artifacts/rights.md",
        "buyer": "artifacts/buyer.md",
        "claims": "artifacts/claims.json",
        "sources": "artifacts/sources/index.json",
    }
    return {
        "assessment_id": ASSESSMENT,
        "created_at": datetime.now(UTC),
        "input": {"prompt": "Can sqlite-vec support a business?"},
        "decision": decision().model_dump(),
        "reasoning": {
            "thesis": "Keep the engine open.",
            "summary": "Fictional.",
            "options": [
                {"id": "O1", "case_for": "a", "case_against": "b", "claims": ["C1"]},
                {"id": "O2", "case_for": "a", "case_against": "b", "claims": ["C2"]},
            ],
            "full": link(refs["reasoning"]),
        },
        "analysis": {
            key: {"summary": "Fictional.", "full": link(refs[key])}
            for key in ("asset", "rights", "buyer")
        },
        "next_test": NEXT_TEST,
        "build_plan": {"option": "O1", "keep_open": ["K1"], "not_authorized": ["deployment"]},
        "open_questions": [],
        "claims": {"count": 3, "full": link(refs["claims"])},
        "sources": {"count": 1, "full": link(refs["sources"])},
        "artifacts": [link(ref) for ref in refs.values()],
    }


def test_result_links_every_artifact_it_names():
    result = SpinoffResult.model_validate(result_payload())
    assert result.schema_version == "condere.spinoff.v1"
    assert len(result.model_dump_json()) < 20_000
    missing = result_payload()
    missing["artifacts"] = missing["artifacts"][1:]
    with pytest.raises(ValidationError, match="missing from the artifact list"):
        SpinoffResult.model_validate(missing)
    mismatched = result_payload()
    mismatched["build_plan"]["option"] = "O2"
    with pytest.raises(ValidationError, match="target the chosen option"):
        SpinoffResult.model_validate(mismatched)
