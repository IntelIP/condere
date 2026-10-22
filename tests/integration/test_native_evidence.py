"""Actual local MinIO and PostgreSQL; no inference, research, or hosted storage."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from condere.runtime.contracts import Citation
from condere.runtime.evidence import EvidenceStore, object_client
from condere.runtime.service import AssessmentService
from condere.runtime.state import AssessmentStore, NotFound, content_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1"
        or os.environ.get("CONDERE_TEST_OBJECTS") != "1",
        reason="Explicit native PostgreSQL and object-store opt-in required",
    ),
]
RECEIPT = Path(__file__).resolve().parents[2] / ".artifacts/infrastructure/native-evidence.json"
ORIGINAL = b"<p>Fictional evidence retained outside the sandbox.</p>"
TEXT = "Fictional evidence: café. This does not establish demand."


@pytest.fixture
def native_evidence():
    if os.environ.get("CONDERE_EVIDENCE_ENDPOINT") != "http://127.0.0.1:19000":
        pytest.fail("This check requires the task's dedicated loopback evidence service.")
    state = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    state.setup()
    client = object_client(os.environ)
    try:
        bucket = os.environ["CONDERE_EVIDENCE_BUCKET"]
        client.head_bucket(Bucket=bucket)
        yield EvidenceStore(state, client, bucket)
    finally:
        client.close()


def test_native_sources_are_immutable_and_owner_checked(native_evidence, monkeypatch):
    evidence = native_evidence
    owner, campaign = "native-evidence-" + str(uuid4()), str(uuid4())
    evidence.state.authorize_campaign(owner, campaign, 0, {"basis": "native storage; no providers"})
    case = evidence.state.admit(
        owner,
        campaign,
        "original",
        {"original": "Fictional source storage check."},
        policy_version="native-storage-test",
        method_version="native-storage-test",
    )
    timestamp = datetime.now(UTC)
    arguments = dict(
        kind="web",
        origin="https://example.com/native-storage-fixture",
        original=ORIGINAL,
        text=TEXT,
        media_type="text/html",
        provenance={"provider": "simulation"},
        limitations=["Fictional material; actual local storage."],
        retrieved_at=timestamp,
    )
    source = evidence.save_source(owner, case["id"], **arguments)
    assert evidence.save_source(owner, case["id"], **arguments) == source
    with pytest.raises(ClientError) as rejected:
        evidence.client.put_object(
            Bucket=evidence.bucket,
            Key=evidence._key(case["id"], source.original_sha256),
            Body=b"Must not replace the original.",
            IfNoneMatch="*",
        )
    assert str(rejected.value.response["Error"]["Code"]) in {"412", "PreconditionFailed"}
    assert evidence.original(owner, case["id"], source.id) == ORIGINAL
    citation = Citation.model_validate(evidence.passage(owner, case["id"], source.id)["citation"])
    assert citation.quote == TEXT
    evidence.verify_citation(owner, case["id"], citation)
    assert evidence.state.operations(owner, case["id"]) == []
    record = {
        "owner_id": owner,
        "assessment_id": case["id"],
        "source_id": source.id,
        "original_sha256": source.original_sha256,
        "text_sha256": source.text_sha256,
        "source_record_hash": evidence.state.records(owner, case["id"], kind="source")[0][
            "content_hash"
        ],
        "storage": "native MinIO",
        "research": "simulation",
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(record, indent=2) + "\n")

    def forbidden(**kwargs):
        pytest.fail("Another owner's request must fail before accessing the object store.")

    monkeypatch.setattr(evidence.client, "get_object", forbidden)
    with pytest.raises(NotFound):
        evidence.original("different-owner", case["id"], source.id)


@pytest.mark.skipif(
    os.environ.get("CONDERE_TEST_OBJECT_RESTART") != "1",
    reason="Run this check only after stopping and restarting the native evidence service",
)
def test_saved_source_after_actual_server_restart(native_evidence):
    record = json.loads(RECEIPT.read_text())
    evidence = native_evidence
    owner, case, source = record["owner_id"], record["assessment_id"], record["source_id"]
    original = evidence.original(owner, case, source)
    assert original == ORIGINAL
    assert hashlib.sha256(original).hexdigest() == record["original_sha256"]
    assert evidence.text(owner, case, source) == TEXT
    assert (
        evidence.state.records(owner, case, kind="source")[0]["content_hash"]
        == (record["source_record_hash"])
    )
    assert evidence.state.operations(owner, case) == []
    (RECEIPT.parent / "native-evidence-after-restart.json").write_text(
        json.dumps(record | {"retrieved_after_operator_restart": True}, indent=2) + "\n"
    )
    pipeline = json.loads((RECEIPT.parent / "native-mcp.json").read_text())
    service = AssessmentService(evidence.state, evidence, execution_mode="simulation")
    report = service.report(pipeline["owner_id"], pipeline["assessment_id"]).model_dump(mode="json")
    assert content_hash(report) == pipeline["report_hash"]
    saved = evidence.original(
        pipeline["owner_id"], pipeline["assessment_id"], pipeline["source_id"]
    )
    assert hashlib.sha256(saved).hexdigest() == pipeline["source_sha256"]
    assert evidence.state.operations(pipeline["owner_id"], pipeline["assessment_id"]) == []
    (RECEIPT.parent / "native-mcp-after-restart.json").write_text(
        json.dumps(pipeline | {"retrieved_after_operator_restart": True}, indent=2) + "\n"
    )
