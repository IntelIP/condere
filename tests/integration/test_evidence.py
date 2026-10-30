"""Real PostgreSQL metadata with a simulated object service; not a live storage receipt."""

import io
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from condere.runtime.contracts import Citation
from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.state import AssessmentStore, NotFound

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


def test_citations_resolve_from_source_and_exact_quote(evidence_case):
    evidence, owner, case = evidence_case
    source = save(evidence, owner, case)
    payload = {
        "conclusion": "x",
        "nested": [{"citations": [{"source_id": source.id, "quote": "Second passage."}]}],
        "citations": [{"source_id": source.id, "quote": "Fixture source,\n not  market evidence."}],
    }
    resolved = evidence.resolve_citations(owner, case, payload)
    exact = Citation.model_validate(resolved["citations"][0])
    assert exact.quote == "Fixture source, not market evidence."
    assert exact.text_sha256 == source.text_sha256 and exact.start == 2
    evidence.verify_citation(owner, case, exact)
    nested = Citation.model_validate(resolved["nested"][0]["citations"][0])
    evidence.verify_citation(owner, case, nested)
    with pytest.raises(EvidenceError, match="not found"):
        evidence.resolve_citations(
            owner, case, {"citations": [{"source_id": source.id, "quote": "Invented demand."}]}
        )
    with pytest.raises(EvidenceError, match="cited passage"):
        evidence.verify_citation(owner, case, Citation(source_id=source.id, quote="Fixture"))
