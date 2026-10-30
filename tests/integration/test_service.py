"""Real assessment records with fictional research and simulated object storage."""

import json
import os
import zipfile
from uuid import uuid4

import pytest
from test_evidence import SimulatedObjects

from condere.runtime.bundle import render_readme
from condere.runtime.contracts import Citation
from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.service import AssessmentService
from condere.runtime.state import (
    AssessmentStore,
    Conflict,
    NotFound,
    RecoveryRequired,
    content_hash,
)

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


def worker(service, owner, case, role):
    record = service.assign_worker(owner, case["id"], case["generation"], role)
    return service.state.attach_machine(
        owner, case["id"], case["generation"], record["id"], "simulated-machine-" + role
    )


def saved_source(service, owner, case, text=b"Operators report upgrades keep breaking."):
    return service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fictional",
        original=text,
        text=text.decode(),
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["No live research."],
    )


def publish(service, owner, case, source, lead=None):
    from test_coordinator import publish_spinoff

    return publish_spinoff(service, owner, case, source.id, lead)


def test_complete_report_and_portable_export_use_same_saved_result(setup_case, tmp_path):
    service, owner, case = setup_case
    source = saved_source(service, owner, case)
    result = publish(service, owner, case, source)
    assert service.state.get(owner, case["id"])["status"] == "completed"
    assert result.input.prompt == "  Assess this fictional technology.\n"
    before = service.state.spending(owner, case["id"])
    reopened = AssessmentService(
        AssessmentStore(os.environ["CONDERE_DATABASE_URL"]),
        service.evidence,
        execution_mode="simulation",
    )
    retrieved = reopened.report(owner, case["id"])
    assert retrieved == result
    output = tmp_path / "assessment.zip"
    reopened.export(owner, case["id"], output)
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("assessment.json")) == result.model_dump(mode="json")
        assert archive.read("README.md").decode() == render_readme(result)
        assert archive.read(f"artifacts/sources/{source.id}.txt") == (
            b"Operators report upgrades keep breaking."
        )
    with pytest.raises(FileExistsError):
        reopened.export(owner, case["id"], output)
    assert output.is_file()
    with pytest.raises(NotFound):
        reopened.export("another-owner", case["id"], tmp_path / "forbidden.zip")
    assert not (tmp_path / "forbidden.zip").exists()
    assert service.state.spending(owner, case["id"]) == before


def test_worker_assignments_control_acceptance(setup_case):
    from test_coordinator import BUILD_PLAN, analysis_payload

    service, owner, case = setup_case
    source = saved_source(service, owner, case)
    lead = worker(service, owner, case, "lead")
    assert "submit_analysis" in lead["assignment"]["permitted_tools"]
    assert "submit_build_plan" not in lead["assignment"]["permitted_tools"]
    planner = worker(service, owner, case, "planner")
    assert planner["assignment"]["skills"][0] == "build-plan"
    assert "submit_analysis" not in planner["assignment"]["permitted_tools"]
    with pytest.raises(Conflict, match="all assigned"):
        service.finish_worker(owner, case["id"], case["generation"], lead["id"])
    with pytest.raises(Conflict, match="running assignment"):
        service.submit_analysis(
            owner, case["id"], case["generation"], planner["id"], analysis_payload(source.id)
        )
    with pytest.raises(Conflict, match="follows the decision"):
        service.submit_build_plan(owner, case["id"], case["generation"], planner["id"], BUILD_PLAN)
    with pytest.raises(ValueError, match="Unknown private worker role"):
        service.assign_worker(owner, case["id"], case["generation"], "technical")
    service.state.stop(owner, case["id"], cancel=True)
    with pytest.raises(Conflict, match="not running"):
        service.submit_analysis(
            owner, case["id"], case["generation"], lead["id"], analysis_payload(source.id)
        )
    resumed = service.begin(owner, case["id"], resume=True)
    with pytest.raises(Conflict):
        service.state.append_record(
            owner,
            case["id"],
            "analysis",
            "main",
            analysis_payload(source.id),
            generation=resumed["generation"],
            worker_id=lead["id"],
        )
    assert service.state.records(owner, case["id"], kind="analysis") == []


def test_atomic_publication_refuses_a_version_change_after_validation(setup_case, monkeypatch):
    service, owner, case = setup_case
    source = saved_source(service, owner, case)
    original_publish = service.state.publish_dossier

    def changed_during_publication(owner, assessment_id, generation, payload, expected):
        service.state.append_record(
            owner,
            assessment_id,
            "build_plan",
            "main",
            {"changed": "concurrently in this simulation"},
            supersedes_hash=expected["build_plan:main"],
        )
        return original_publish(owner, assessment_id, generation, payload, expected)

    monkeypatch.setattr(service.state, "publish_dossier", changed_during_publication)
    with pytest.raises(Conflict, match="changed before publication"):
        publish(service, owner, case, source)
    with pytest.raises(NotFound):
        service.report(owner, case["id"])


def test_pending_dispatch_prevents_delivery_and_late_billing_keeps_saved_result(
    setup_case, monkeypatch
):
    service, owner, case = setup_case
    source = saved_source(service, owner, case)
    operation = service.state.reserve_operation(
        owner, case["id"], case["generation"], "simulated-model", "simulation", {}, 800_000
    )
    service.state.dispatch(
        owner, case["id"], case["generation"], operation["id"], {"request_id": "fixture"}
    )
    finalize = service.finalize_spinoff
    attempts = []

    def finalize_after_settling(owner, assessment_id, generation):
        if not attempts:
            attempts.append("pending")
            with pytest.raises(RecoveryRequired):
                finalize(owner, assessment_id, generation)
            service.state.finish_operation(
                owner, assessment_id, operation["id"], {"response": "fixture"}, succeeded=True
            )
        return finalize(owner, assessment_id, generation)

    monkeypatch.setattr(service, "finalize_spinoff", finalize_after_settling)
    result = publish(service, owner, case, source)
    assert attempts == ["pending"]
    assert service.state.spending(owner, case["id"])["budget"]["pending_microusd"] == 800_000
    service.state.settle_cost(
        owner, case["id"], operation["id"], 1_200_000, {"basis": "simulated late upstream overrun"}
    )
    assert service.state.get(owner, case["id"])["status"] == "completed"
    assert service.report(owner, case["id"]) == result
    assert service.state.spending(owner, case["id"])["budget"]["settled_microusd"] == 1_200_000


def test_failed_export_keeps_report_and_removes_only_its_partial_file(setup_case, tmp_path):
    service, owner, case = setup_case
    source = saved_source(service, owner, case)
    result = publish(service, owner, case, source)
    service.evidence.client.objects[
        (service.evidence.bucket, service.evidence._key(case["id"], source.text_sha256))
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


REPOSITORY = "https://github.com/example/fictional"
COMMIT = "a" * 40


@pytest.fixture
def reuse_owner():
    store = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    store.setup()
    evidence = EvidenceStore(store, SimulatedObjects(), "condere-reuse-test")
    service = AssessmentService(store, evidence, execution_mode="simulation")

    def owner_with_campaign():
        owner, campaign = "reuse-test-" + str(uuid4()), str(uuid4())
        store.authorize_campaign(
            owner, campaign, 1_000_000, {"basis": "simulation"}, assessment_limit=10
        )
        return owner, campaign

    return service, owner_with_campaign


def pinned_case(service, owner, campaign, commit=COMMIT):
    admitted = service.admit(
        owner, campaign, str(uuid4()), {"original_input": "Assess the fictional repository."}
    )
    case = service.begin(owner, admitted["id"])
    service.state.append_record(
        owner,
        case["id"],
        "repository_pin",
        content_hash(REPOSITORY),
        {"repository": REPOSITORY, "commit": commit, "archive_sha256": "b" * 64},
    )
    return case


def completed_code_analysis(service, owner, campaign, commit=COMMIT):
    """A completed fictional assessment whose code analyst and extractor saved claims."""
    case = pinned_case(service, owner, campaign, commit)
    code = "FILE: LICENSE\nReleased under the MIT License.\nFILE: src/vec.c\nint vec_dims = 8;"
    repository = service.evidence.save_source(
        owner,
        case["id"],
        kind="repository",
        origin=REPOSITORY,
        original=b"fictional archive bytes " + commit.encode(),
        text=code,
        media_type="application/gzip",
        provenance={"commit": commit, "manifest": [{"path": "LICENSE", "kind": "file"}]},
        limitations=["Only the named files were read."],
    )
    web = saved_source(service, owner, case)
    lead = worker(service, owner, case, "lead")

    def claim(statement, source, quote):
        return {
            "statement": statement,
            "status": "observed",
            "citations": [{"source_id": source.id, "quote": quote}],
        }

    code_claims = service.submit_claims(
        owner,
        case["id"],
        case["generation"],
        lead["id"],
        [
            claim("The code is MIT licensed.", repository, "Released under the MIT License."),
            claim("Vectors have eight dimensions.", repository, "int vec_dims = 8;"),
            claim("Upgrades break.", web, "upgrades keep breaking"),
            {"statement": "It permits hosting.", "status": "modelled"},
        ],
        origin="code-analyst",
    )
    service.submit_claims(
        owner,
        case["id"],
        case["generation"],
        lead["id"],
        [claim("The license is MIT.", repository, "MIT License")],
        origin="extractor",
    )
    publish(service, owner, case, repository, lead=lead)
    candidates = [item["candidate"] for item in code_claims["accepted"]]
    return case, repository, candidates


def test_reuse_copies_verified_code_claims_and_their_sources(reuse_owner):
    from test_coordinator import analysis_payload

    service, owner_with_campaign = reuse_owner
    owner, campaign = owner_with_campaign()
    completed_code_analysis(service, owner, campaign)
    previous, repository, candidates = completed_code_analysis(service, owner, campaign)
    case = pinned_case(service, owner, campaign)

    [reuse] = service.reuse_code_claims(owner, case["id"])
    assert reuse["reused_from"] == previous["id"]  # The most recent compatible assessment.
    assert reuse["candidates"] == candidates[:2]  # Code claims citing the commit only.
    assert reuse["sources"] == [repository.id] and reuse["skipped"] == []
    assert service.evidence.source(owner, case["id"], repository.id) == repository
    assert service.evidence.original(owner, case["id"], repository.id) == (
        service.evidence.original(owner, previous["id"], repository.id)
    )
    listed = service.candidate_claims(owner, case["id"])
    assert [item["candidate"] for item in listed] == candidates[:2]
    assert {(item["origin"], item["reused_from"]) for item in listed} == {
        ("code-analyst", previous["id"])
    }
    for record in service.state.records(owner, case["id"], kind="candidate_claim"):
        assert record["payload"]["code_method"] == service.code_method
        for citation in record["payload"]["citations"]:
            service.evidence.verify_citation(owner, case["id"], Citation.model_validate(citation))
    assert service.reused_code_analysis(owner, case["id"]) == [
        {
            "repository": REPOSITORY,
            "commit": COMMIT,
            "reused_from": previous["id"],
            "claims": 2,
            "sources": 1,
        }
    ]
    before = service.state.records(owner, case["id"])
    assert service.reuse_code_claims(owner, case["id"]) == []  # Recorded once per repository.
    assert service.state.records(owner, case["id"]) == before

    # A code analyst re-saving a reused claim gets the same ID; the lead cites it by ID.
    lead = worker(service, owner, case, "lead")
    again = service.submit_claims(
        owner,
        case["id"],
        case["generation"],
        lead["id"],
        [
            {
                "statement": "The code is MIT licensed.",
                "status": "observed",
                "citations": [
                    {"source_id": repository.id, "quote": "Released under the MIT License."}
                ],
            }
        ],
        origin="code-analyst",
    )
    assert again == {"accepted": [{"index": 0, "candidate": candidates[0]}], "rejected": []}
    payload = analysis_payload(repository.id, "MIT License")
    payload["claims"][0] = {"id": "C1", "candidate": candidates[0]}
    service.submit_analysis(owner, case["id"], case["generation"], lead["id"], payload)
    assert service.analysis(owner, case["id"]).claim("C1").statement == "The code is MIT licensed."


def test_no_reuse_across_commits_owners_or_code_methods(reuse_owner):
    service, owner_with_campaign = reuse_owner
    owner, campaign = owner_with_campaign()
    completed_code_analysis(service, owner, campaign)

    other_commit = pinned_case(service, owner, campaign, commit="c" * 40)
    assert service.reuse_code_claims(owner, other_commit["id"]) == []

    stranger, stranger_campaign = owner_with_campaign()
    other_owner = pinned_case(service, stranger, stranger_campaign)
    assert service.reuse_code_claims(stranger, other_owner["id"]) == []

    changed = AssessmentService(service.state, service.evidence, execution_mode="simulation")
    changed.code_method = "a-changed-code-analyst-prompt-or-skill"
    other_method = pinned_case(changed, owner, campaign)
    assert changed.reuse_code_claims(owner, other_method["id"]) == []

    for owner_id, case in ((owner, other_commit), (stranger, other_owner), (owner, other_method)):
        assert service.state.records(owner_id, case["id"], kind="candidate_claim") == []
        assert service.state.records(owner_id, case["id"], kind="source") == []
        assert service.reused_code_analysis(owner_id, case["id"]) == []


def test_the_request_and_any_pasted_text_are_saved_once_as_a_citable_source(setup_case):
    service, owner, case = setup_case
    first = service.save_original_input(owner, case["id"])
    assert service.save_original_input(owner, case["id"]) == first
    [saved] = [
        record["payload"]
        for record in service.state.records(owner, case["id"], kind="source")
        if record["payload"]["id"] == first
    ]
    assert saved["kind"] == "user_file" and saved["origin"] == "condere://original-input"
    cited = service.evidence._resolve(
        owner, case["id"], {"source_id": first, "quote": "Assess this fictional technology."}
    )
    service.evidence.verify_citation(owner, case["id"], Citation.model_validate(cited))
