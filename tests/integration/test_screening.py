"""Qualification saves usable results and deepens only through the existing full contract."""

import json
import os
import zipfile
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_coordinator import native_case as native_case
from test_sandbox import RehearsalModel
from test_service import setup_case as setup_case
from test_service import worker

from condere.runtime.contracts import Citation, OpportunityScreen
from condere.runtime.dossier import render_markdown
from condere.runtime.evidence import EvidenceError
from condere.runtime.state import Conflict
from condere.runtime.tools import worker_tools

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


def screen_payload(recommendation="needs_customer_evidence"):
    missing = dict(
        conclusion="No customer observations in this fixture.",
        availability="unavailable",
        limitations=["Simulation only."],
    )
    return dict(
        recommendation=recommendation,
        summary="Customer evidence is needed before a delivery commitment.",
        candidates=[
            dict(
                name="Proposed integration",
                customer="Hypothesized maintainer",
                problem=missing,
                commercial_evidence=missing,
                alternatives=missing,
            )
        ],
        selected_candidate="Proposed integration",
        decision_reasons=["This fixture does not establish demand."],
        material_gaps=["Buyer problem and purchasing evidence."],
        next_checks=[
            dict(
                question="What changes the decision?",
                action="Inspect the missing fact.",
                execution="agent_research" if recommendation == "deepen" else "customer_input",
                decision_impact="Determine whether deeper investigation is useful.",
            )
        ],
    )


def test_screen_saves_distinct_result_and_preserves_export(setup_case, tmp_path):
    service, owner, case = setup_case
    assigned = worker(service, owner, case, "screen")
    screen = screen_payload()
    service.submit_screen(owner, case["id"], case["generation"], assigned["id"], screen)
    service.finish_worker(owner, case["id"], case["generation"], assigned["id"])
    result = service.finalize_screen(owner, case["id"], case["generation"])
    assert result.schema_version == "condere.screen.v1"
    assert not result.deep_assessment_completed
    assert service.report(owner, case["id"]).model_dump() == result.model_dump()
    assert "full Technical/Market/Business assessment has not run" in render_markdown(result)
    assert "customer input" in render_markdown(result)
    assert service.state.get(owner, case["id"])["status"] == "completed"
    archive = service.export(owner, case["id"], tmp_path / "screen.zip")
    with zipfile.ZipFile(archive) as saved:
        assert json.loads(saved.read("assessment.json"))["deep_assessment_completed"] is False
        assert b"opportunity screen" in saved.read("assessment.html")


def test_screen_cannot_bypass_deep_work_or_citation_checks(setup_case):
    service, owner, case = setup_case
    assigned = worker(service, owner, case, "screen")
    payload = screen_payload("deepen")
    service.submit_screen(owner, case["id"], case["generation"], assigned["id"], payload)
    with pytest.raises(Conflict, match="deeper investigation"):
        service.finalize_screen(owner, case["id"], case["generation"])
    payload["next_checks"][0]["execution"] = "customer_input"
    with pytest.raises(ValueError, match="executable research"):
        OpportunityScreen.model_validate(payload)
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fixture",
        original=b"Fictional account.",
        text="Fictional account.",
        media_type="text/plain",
        provenance={},
        limitations=["Fictional test source."],
    )
    payload = screen_payload()
    citation = service.evidence.passage(owner, case["id"], source.id)["citation"]
    citation["quote"] = "A different unsupported claim."
    payload["candidates"][0]["problem"] = dict(
        conclusion="Unsupported",
        availability="available",
        basis="source_observed",
        source_ids=[source.id],
        citations=[citation],
    )
    with pytest.raises((EvidenceError, ValueError)):
        service.submit_screen(owner, case["id"], case["generation"], assigned["id"], payload)


def test_saved_source_search_returns_exact_reusable_passages(setup_case, tmp_path):
    service, owner, case = setup_case
    assigned = worker(service, owner, case, "screen")
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/incident",
        original="İ observed a recurring manual export.".encode(),
        text="İ observed a recurring manual export.",
        media_type="text/plain",
        provenance={},
        limitations=["Fictional test source."],
    )
    tools = {
        tool.name: tool
        for tool in worker_tools(
            service,
            owner=owner,
            assessment_id=case["id"],
            generation=case["generation"],
            worker_id=assigned["id"],
            research=None,
            sandbox=SimpleNamespace(id=assigned["machine_id"]),
            repository_cache=tmp_path,
            allowed_repositories=set(),
        )
    }
    result = tools["search_saved_sources"].invoke({"terms": ["MANUAL EXPORT"]})
    citation = Citation.model_validate(result["matches"][0]["citation"])
    assert citation.source_id == source.id
    service.evidence.verify_citation(owner, case["id"], citation)
    assert service.state.operations(owner, case["id"]) == []
    assert tools["search_saved_sources"].invoke({"terms": ["unseen"]})["matches"] == []


@pytest.mark.parametrize("native_case", ["adaptive"], indirect=True)
@pytest.mark.parametrize("recommendation", ["needs_customer_evidence", "deepen"])
@pytest.mark.skipif(
    os.environ.get("CONDERE_TEST_SANDBOX") != "1", reason="Explicit native sandbox opt-in required"
)
@pytest.mark.asyncio
async def test_native_screen_routes_and_preserves_deep_contract(native_case, recommendation):
    service, owner, case, make, factory, calls, created, artifacts = native_case
    model = RehearsalModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="submit_screen",
                        id="screen-1",
                        args={"payload": screen_payload(recommendation)},
                    )
                ],
            ),
            AIMessage(content="Fixture qualification submitted."),
        ]
    )

    def models(context):
        return model if context.worker_role == "screen" else factory(context)

    result = await make(model_factory=models).run(owner, case["id"])
    if recommendation == "deepen":
        assert len(result.findings.questions) == 26
        assert result.screening.recommendation == "deepen"
        assert len(calls) == 27 and calls[-1] == ("review", 1)
    else:
        assert not result.deep_assessment_completed
        assert calls == []
    assert service.state.get(owner, case["id"])["status"] == "completed"
