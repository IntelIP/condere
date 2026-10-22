"""Real PostgreSQL metadata with a simulated object service; not a live storage receipt."""

import io
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from condere.runtime.contracts import (
    CAPABILITY_CATEGORIES,
    QUESTION_IDS,
    QUESTIONS,
    TECHNICAL_FIELDS,
    AssessmentFindings,
    Citation,
)
from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.state import AssessmentStore, Conflict, NotFound

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


class SimulatedObjects:
    def __init__(self):
        self.objects = {}
        self.calls = 0
        self.fail_at = None

    def put_object(self, **request):
        self.calls += 1
        if self.calls == self.fail_at:
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "PutObject")
        key = (request["Bucket"], request["Key"])
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = request["Body"]
        return {}

    def get_object(self, **request):
        self.calls += 1
        key = (request["Bucket"], request["Key"])
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[key]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture
def evidence_case():
    state = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    state.setup()
    owner, campaign = "evidence-test-" + str(uuid4()), str(uuid4())
    state.authorize_campaign(
        owner, campaign, 0, {"basis": "simulated storage test; no provider calls"}
    )
    case = state.admit(
        owner,
        campaign,
        "intake",
        {"original": "fixture"},
        policy_version="test",
        method_version="test",
    )
    objects = SimulatedObjects()
    return EvidenceStore(state, objects, "condere-test-evidence"), owner, case["id"]


def save(evidence, owner, case):
    return evidence.save_source(
        owner,
        case,
        kind="web",
        origin="https://example.com/fixture",
        original=b"<p>Fixture source, not market evidence.</p>",
        text="  Fixture source, not market evidence.\nSecond passage.\n",
        media_type="text/html",
        provenance={"provider": "simulation"},
        limitations=["Fictional source used only for a contract check."],
        retrieved_at=datetime(2026, 10, 21, tzinfo=UTC),
    )


def test_originals_and_passages_survive_reopening_without_invoking_research(evidence_case):
    evidence, owner, case = evidence_case
    source = save(evidence, owner, case)
    repeated = save(evidence, owner, case)
    assert repeated.id == source.id
    assert len(evidence.state.records(owner, case, kind="source")) == 1
    reopened = EvidenceStore(
        AssessmentStore(os.environ["CONDERE_DATABASE_URL"]), evidence.client, evidence.bucket
    )
    window = reopened.passage(owner, case, source.id, limit=12)
    quote = Citation.model_validate(window["citation"])
    assert quote.quote == "  Fixture so"
    assert window["next_offset"] == 12
    reopened.verify_citation(owner, case, quote)
    assert reopened.original(owner, case, source.id).startswith(b"<p>")
    assert reopened.state.operations(owner, case) == []
    before = evidence.client.calls
    with pytest.raises(NotFound):
        reopened.original("another-owner", case, source.id)
    assert evidence.client.calls == before


def test_changed_evidence_and_invented_quotes_are_rejected(evidence_case):
    evidence, owner, case = evidence_case
    source = save(evidence, owner, case)
    citation = Citation.model_validate(evidence.passage(owner, case, source.id)["citation"])
    with pytest.raises(EvidenceError, match="cited passage"):
        evidence.verify_citation(
            owner, case, citation.model_copy(update={"quote": "Unfounded customer demand."})
        )
    key = (evidence.bucket, evidence._key(case, source.text_sha256))
    evidence.client.objects[key] = b"Changed evidence"
    with pytest.raises(EvidenceError, match="content hash"):
        evidence.verify_citation(owner, case, citation)
    assert len(evidence.state.records(owner, case, kind="source")) == 1


def test_failed_object_write_does_not_publish_a_source_record(evidence_case):
    evidence, owner, case = evidence_case
    evidence.client.fail_at = 2
    with pytest.raises(EvidenceError, match="could not be saved"):
        save(evidence, owner, case)
    assert evidence.state.records(owner, case, kind="source") == []
    evidence.client.fail_at = None
    source = save(evidence, owner, case)
    assert evidence.text(owner, case, source.id).startswith("  Fixture")


def full_fixture(metric):
    unknown = dict(
        conclusion="Not established in this fixture.",
        availability="unavailable",
        limitations=["Simulated evidence only."],
    )
    questions = [
        dict(
            question_id=key,
            conclusion="Fictional hypothesis.",
            availability="unavailable" if key == "B3" else "available",
            basis=None if key == "B3" else "modelled",
            assumptions=[] if key == "B3" else ["Simulation, not business evidence."],
            limitations=["No live research."],
            details={
                field: None if key == "B3" else "Fictional proposed detail."
                for field in QUESTIONS[key]["required_fields"]
            },
            next_action="Human review only.",
        )
        for key in QUESTION_IDS
    ]
    return AssessmentFindings.model_validate(
        dict(
            technical=dict(
                findings={field: unknown for field in TECHNICAL_FIELDS},
                capabilities=[
                    dict(
                        scenario="fixture",
                        category=category,
                        name=category,
                        status="unavailable",
                        value=None,
                        finding=unknown,
                    )
                    for category in CAPABILITY_CATEGORIES
                ],
                inspection_limitations=["No repository inspection in this simulation."],
                metrics=[metric],
            ),
            questions=questions,
        )
    )


def test_measurements_require_an_immutable_receipt_bound_to_the_saved_source(evidence_case):
    evidence, owner, case = evidence_case
    source = evidence.save_source(
        owner,
        case,
        kind="measurement",
        origin="fixture://measurement",
        original=b"Simulated p95: 1.2 seconds.",
        text="Simulated p95: 1.2 seconds.",
        media_type="text/plain",
        provenance={"receipt_id": "fixture-receipt"},
        limitations=["Simulated measurement, not a benchmark."],
    )
    citation = evidence.passage(owner, case, source.id)["citation"]
    metric = dict(
        scenario="fixture",
        subject="example",
        name="p95_latency",
        value="1.2",
        unit="second",
        basis="measured",
        source_ids=[source.id],
        citations=[citation],
        receipt_id="fixture-receipt",
        product_version="fixture-1",
        conditions=dict(
            workload="simulated",
            fixture_id="fixture-1",
            measurement_protocol_id="fixture-protocol",
            baseline_id="fixture-baseline",
            region="local-fixture",
        ),
    )
    findings = full_fixture(metric)
    with pytest.raises(EvidenceError, match="source-bound receipt"):
        evidence.verify_findings(owner, case, findings)
    measured = findings.technical.metrics[0].model_dump(
        mode="json",
        include={"scenario", "subject", "name", "value", "unit", "product_version", "conditions"},
    )
    receipt = dict(source_id=source.id, original_sha256=source.original_sha256, metric=measured)
    accepted = evidence.state.append_record(
        owner, case, "measurement_receipt", "fixture-receipt", receipt
    )
    evidence.verify_findings(owner, case, findings)
    with pytest.raises(Conflict, match="cannot be revised"):
        evidence.state.append_record(
            owner,
            case,
            "measurement_receipt",
            "fixture-receipt",
            receipt | {"metric": measured | {"value": "0.1"}},
            supersedes_hash=accepted["content_hash"],
        )
    with pytest.raises(EvidenceError, match="matching protected"):
        evidence.verify_findings(owner, case, full_fixture(metric | {"value": "0.1"}))
    evidence.client.objects[(evidence.bucket, evidence._key(case, source.original_sha256))] = (
        b"Changed after acceptance"
    )
    with pytest.raises(EvidenceError, match="content hash"):
        evidence.verify_findings(owner, case, findings)
