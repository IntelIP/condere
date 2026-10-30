"""Real assessment records with fictional research and simulated object storage."""

import json
import os
import zipfile
from uuid import uuid4

import pytest
from test_evidence import SimulatedObjects

from condere.runtime.contracts import QUESTION_IDS, QUESTIONS
from condere.runtime.dossier import render_json, render_markdown
from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.service import AssessmentService
from condere.runtime.state import AssessmentStore, Conflict, NotFound, RecoveryRequired

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


@pytest.fixture
def setup_case():
    store = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    store.setup()
    evidence = EvidenceStore(store, SimulatedObjects(), "condere-service-test")
    service = AssessmentService(store, evidence, execution_mode="simulation")
    owner, campaign = "service-test-" + str(uuid4()), str(uuid4())
    store.authorize_campaign(
        owner, campaign, 1_000_000, {"basis": "simulation; no provider spending"}
    )
    admitted = service.admit(
        owner,
        campaign,
        "original",
        {
            "original_input": "  Assess this fictional technology.\n",
            "technology": "Fictional library",
        },
    )
    case = service.begin(owner, admitted["id"])
    return service, owner, case


def worker(service, owner, case, role, question_id=None):
    record = service.assign_worker(owner, case["id"], case["generation"], role, question_id)
    return service.state.attach_machine(
        owner,
        case["id"],
        case["generation"],
        record["id"],
        "simulated-machine-" + role + (question_id or ""),
    )


def stage(identity):
    return QUESTIONS[identity]["stage"]


def unavailable(identity):
    """Fixture answers are modelled where allowed; observed-only questions stay unavailable."""
    return identity in {"M3", "B3"} or "modelled" not in QUESTIONS[identity]["allowed_bases"]


def question(identity):
    missing = unavailable(identity)
    return dict(
        question_id=identity,
        conclusion="Fictional finding for integration checks only.",
        availability="unavailable" if missing else "available",
        basis=None if missing else "modelled",
        details={
            key: None if missing else "A hypothesis, not an observed business fact."
            for key in QUESTIONS[identity]["required_fields"]
        },
        assumptions=[] if missing else ["No real customers or research are represented."],
        limitations=["Simulated evidence only."],
        next_action="Review missing evidence before choosing a test.",
    )


def accept_findings(service, owner, case):
    workers = {
        identity: worker(service, owner, case, stage(identity), identity)
        for identity in QUESTION_IDS
    }
    for identity in QUESTION_IDS:
        service.submit_question(
            owner, case["id"], case["generation"], workers[identity]["id"], question(identity)
        )
    return workers


def review(service, owner, case):
    context = service.review_context(owner, case["id"])
    return dict(
        items=[
            dict(
                target=target,
                reviewed_hash=digest,
                status="insufficient" if unavailable(target) else "qualified",
                reasoning="This fixture is not proof of customer demand.",
                qualifications=["Simulated inputs and unverified assumptions."],
            )
            for target, digest in context["reviewed_hashes"].items()
        ],
        recommendation="insufficient_evidence",
        summary="No business decision can be established from this fictional fixture.",
        decision_reasons=["Demand and operating costs remain unknown."],
        material_gaps=["Real evidence has not been collected."],
        contrary_evidence=["No buyer commitment is established."],
        handoff=dict(
            candidate_architecture=None,
            alternatives=["Keep the current manual process."],
            versions_and_licenses=[],
            assumptions=["All findings are fictional."],
            constraints=["Human review is required."],
            proposed_implementation_scope=[],
            next_validation_tasks=["Propose a bounded customer-problem test."],
            unresolved_decisions=["Whether this problem matters."],
            limitations=["No implementation recommendation is established."],
        ),
    )


def ready_for_finalization(service, owner, case):
    workers = accept_findings(service, owner, case)
    for record in workers.values():
        service.finish_worker(owner, case["id"], case["generation"], record["id"])
    reviewer = worker(service, owner, case, "review")
    service.submit_review(
        owner, case["id"], case["generation"], reviewer["id"], review(service, owner, case)
    )
    service.finish_worker(owner, case["id"], case["generation"], reviewer["id"])
    return workers, reviewer


def test_complete_report_and_portable_export_use_same_saved_findings(setup_case, tmp_path):
    service, owner, case = setup_case
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fictional",
        original=b"Original fixture bytes.",
        text="Original fixture bytes.",
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["No live research."],
    )
    _, reviewer = ready_for_finalization(service, owner, case)
    assert (
        service.finish_worker(owner, case["id"], case["generation"], reviewer["id"])["status"]
        == "completed"
    )
    result = service.finalize(owner, case["id"], case["generation"])
    assert service.state.get(owner, case["id"])["status"] == "completed"
    assert result.brief.original_input == "  Assess this fictional technology.\n"
    assert len(result.findings.questions) == 26
    before = service.state.spending(owner, case["id"])
    reopened = AssessmentService(
        AssessmentStore(os.environ["CONDERE_DATABASE_URL"]),
        service.evidence,
        execution_mode="simulation",
    )
    retrieved = reopened.report(owner, case["id"])
    assert render_json(retrieved) == render_json(result)
    assert reopened.finalize(owner, case["id"], case["generation"]) == retrieved
    markdown = render_markdown(retrieved)
    assert "**Simulation." in markdown
    assert all(f"### {identity}:" in markdown for identity in QUESTION_IDS)
    assert "Human review is required" in markdown
    output = tmp_path / "assessment.zip"
    reopened.export(owner, case["id"], output)
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("assessment.json")) == result.model_dump(mode="json")
        assert archive.read("assessment.md").decode() == markdown
        assert archive.read(f"sources/{source.id}.original") == b"Original fixture bytes."
        assert archive.read(f"sources/{source.id}.txt") == b"Original fixture bytes."
    with pytest.raises(FileExistsError):
        reopened.export(owner, case["id"], output)
    assert output.is_file()
    with pytest.raises(NotFound):
        reopened.export("another-owner", case["id"], tmp_path / "forbidden.zip")
    assert not (tmp_path / "forbidden.zip").exists()
    assert service.state.spending(owner, case["id"]) == before


def test_question_assignments_control_acceptance(setup_case):
    service, owner, case = setup_case
    market = worker(service, owner, case, "market", "M1")
    assert market["assignment"]["skills"] == ["opportunity-framing", "market-evidence"]
    with pytest.raises(Conflict, match="outside"):
        service.submit_question(owner, case["id"], case["generation"], market["id"], question("M2"))
    with pytest.raises(Conflict, match="all assigned"):
        service.finish_worker(owner, case["id"], case["generation"], market["id"])
    technical = worker(service, owner, case, "technical", "T2")
    assert "inspect_repository" in technical["assignment"]["permitted_tools"]
    assert "deployment-analysis" in technical["assignment"]["skills"]
    assert "inspect_repository" not in market["assignment"]["permitted_tools"]
    with pytest.raises(Conflict, match="outside"):
        service.submit_question(
            owner, case["id"], case["generation"], technical["id"], question("T1")
        )
    with pytest.raises(ValueError, match="own stage"):
        service.assign_worker(owner, case["id"], case["generation"], "technical", "M1")
    service.state.stop(owner, case["id"], cancel=True)
    with pytest.raises(Conflict, match="not running"):
        service.submit_question(owner, case["id"], case["generation"], market["id"], question("M1"))
    resumed = service.begin(owner, case["id"], resume=True)
    with pytest.raises(Conflict):
        service.state.append_record(
            owner,
            case["id"],
            "question",
            "M1",
            question("M1"),
            generation=resumed["generation"],
            worker_id=market["id"],
        )
    assert service.state.records(owner, case["id"]) == []


def test_review_cannot_certify_unknowns_or_replace_accepted_originals(setup_case):
    service, owner, case = setup_case
    workers = accept_findings(service, owner, case)
    reviewer = worker(service, owner, case, "review")
    payload = review(service, owner, case)
    bad = dict(payload, items=[dict(item, status="supported") for item in payload["items"]])
    with pytest.raises(Conflict, match="Unavailable"):
        service.submit_review(owner, case["id"], case["generation"], reviewer["id"], bad)
    saved_review = service.submit_review(
        owner, case["id"], case["generation"], reviewer["id"], payload
    )
    previous = service.review_context(owner, case["id"])["reviewed_hashes"]["B1"]
    changed = question("B1") | {"conclusion": "A revised fictional hypothesis, still unverified."}
    service.submit_question(
        owner,
        case["id"],
        case["generation"],
        workers["B1"]["id"],
        changed,
        supersedes_hash=previous,
    )
    for record in [*workers.values(), reviewer]:
        service.finish_worker(owner, case["id"], case["generation"], record["id"])
    with pytest.raises(Conflict, match="current accepted version"):
        service.finalize(owner, case["id"], case["generation"])
    records = service.state.records(owner, case["id"])
    assert (
        len(
            [
                record
                for record in records
                if record["kind"] == "question" and record["record_key"] == "B1"
            ]
        )
        == 2
    )
    assert (
        next(record for record in records if record["kind"] == "review")["content_hash"]
        == saved_review["content_hash"]
    )
    assert service.state.get(owner, case["id"])["status"] == "running"


def test_atomic_publication_refuses_a_version_change_after_validation(setup_case, monkeypatch):
    service, owner, case = setup_case
    ready_for_finalization(service, owner, case)
    original_publish = service.state.publish_dossier

    def changed_during_publication(owner, assessment_id, generation, payload, expected):
        service.state.append_record(
            owner,
            assessment_id,
            "question",
            "B1",
            question("B1") | {"conclusion": "Changed concurrently in this simulation."},
            supersedes_hash=expected["question:B1"],
        )
        return original_publish(owner, assessment_id, generation, payload, expected)

    monkeypatch.setattr(service.state, "publish_dossier", changed_during_publication)
    with pytest.raises(Conflict, match="changed after review"):
        service.finalize(owner, case["id"], case["generation"])
    with pytest.raises(NotFound):
        service.report(owner, case["id"])


def test_pending_dispatch_prevents_delivery_and_late_billing_keeps_saved_dossier(setup_case):
    service, owner, case = setup_case
    ready_for_finalization(service, owner, case)
    operation = service.state.reserve_operation(
        owner, case["id"], case["generation"], "simulated-model", "simulation", {}, 800_000
    )
    service.state.dispatch(
        owner, case["id"], case["generation"], operation["id"], {"request_id": "fixture"}
    )
    with pytest.raises(RecoveryRequired):
        service.finalize(owner, case["id"], case["generation"])
    service.state.finish_operation(
        owner, case["id"], operation["id"], {"response": "fixture"}, succeeded=True
    )
    result = service.finalize(owner, case["id"], case["generation"])
    assert result.spending["budget"]["pending_microusd"] == 800_000
    service.state.settle_cost(
        owner, case["id"], operation["id"], 1_200_000, {"basis": "simulated late upstream overrun"}
    )
    assert service.state.get(owner, case["id"])["status"] == "completed"
    assert service.report(owner, case["id"]) == result
    assert service.state.spending(owner, case["id"])["budget"]["settled_microusd"] == 1_200_000


def test_failed_export_keeps_report_and_removes_only_its_partial_file(setup_case, tmp_path):
    service, owner, case = setup_case
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fixture",
        original=b"Fixture",
        text="Fixture",
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["No live research."],
    )
    ready_for_finalization(service, owner, case)
    result = service.finalize(owner, case["id"], case["generation"])
    service.evidence.client.objects[
        (service.evidence.bucket, service.evidence._key(case["id"], source.original_sha256))
    ] = b"Corrupted fixture"
    target = tmp_path / "partial.zip"
    with pytest.raises(EvidenceError, match="content hash"):
        service.export(owner, case["id"], target)
    assert not target.exists()
    assert service.report(owner, case["id"]) == result


def test_continuation_cannot_change_admitted_policy_or_methods(setup_case):
    service, owner, case = setup_case
    service.state.stop(owner, case["id"])
    before = service.state.spending(owner, case["id"])
    other_mode = AssessmentService(service.state, service.evidence, execution_mode="live")
    with pytest.raises(Conflict, match="originally admitted"):
        other_mode.begin(owner, case["id"], resume=True)
    service.method_version = "changed-method-version"
    with pytest.raises(Conflict, match="originally admitted"):
        service.begin(owner, case["id"], resume=True)
    assert service.state.spending(owner, case["id"]) == before
    assert service.state.get(owner, case["id"])["generation"] == case["generation"]


def test_claims_ledger_saves_verified_claims_and_expands_references(setup_case):
    from test_coordinator import analysis_payload

    service, owner, case = setup_case
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/ledger",
        original=b"Operators report upgrades keep breaking in production.",
        text="Operators report upgrades keep breaking in production.",
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["Fixture."],
    )
    lead = worker(service, owner, case, "lead")
    observed = {
        "statement": "Operators report upgrade breakage.",
        "status": "observed",
        "citations": [{"source_id": source.id, "quote": "upgrades keep breaking"}],
    }
    forged = observed | {"citations": [{"source_id": source.id, "quote": "never said this"}]}
    saved = service.submit_claims(
        owner,
        case["id"],
        case["generation"],
        lead["id"],
        [observed, forged, observed],
        origin="extractor",
    )
    first, duplicate = saved["accepted"]
    assert first["candidate"] == duplicate["candidate"]
    assert [item["index"] for item in saved["rejected"]] == [1]
    assert "not found" in saved["rejected"][0]["error"]
    [listed] = service.candidate_claims(owner, case["id"])
    assert listed["candidate"] == first["candidate"] and listed["source_ids"] == [source.id]
    payload = analysis_payload(source.id)
    payload["claims"][0] = {"id": "C1", "candidate": first["candidate"]}
    service.submit_analysis(owner, case["id"], case["generation"], lead["id"], payload)
    claim = service.analysis(owner, case["id"]).claim("C1")
    assert claim.candidate == first["candidate"] and claim.status == "observed"
    assert claim.citations[0].text_sha256 == source.text_sha256
    payload["claims"][0] = {"id": "C1", "candidate": "X0000000000"}
    with pytest.raises(ValueError, match="unknown candidate"):
        service.submit_analysis(owner, case["id"], case["generation"], lead["id"], payload)
