import pytest
import requests
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langsmith.utils import LangSmithNotFoundError
from requests.models import Response

from condere.runtime import tracing as tracing_module
from condere.runtime.tracing import Tracing


@pytest.fixture
def sent(monkeypatch):
    """Capture LangSmith HTTP bodies; no request leaves the process."""
    bodies = []

    def send(session, request, **kwargs):
        body = request.body or b""
        bodies.append(body.encode() if isinstance(body, str) else body)
        response = Response()
        response.status_code, response._content, response.request = 202, b"{}", request
        response.headers["content-type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    return bodies


def configured(**values):
    return {"LANGSMITH_API_KEY": "lsv2-test", **values}


@pytest.mark.parametrize(
    "environment",
    [
        {"LANGSMITH_PROJECT": "condere-dev"},
        configured(),
        configured(LANGSMITH_AGENT_ID="condere"),
        configured(LANGSMITH_PROJECT="condere-dev", LANGSMITH_AGENT_ID="condere"),
        configured(
            LANGSMITH_PROJECT="condere-dev",
            LANGSMITH_AGENT_ID="condere",
            LANGSMITH_AGENT_ENVIRONMENT="development",
        ),
    ],
)
def test_configuration_requires_key_and_one_destination(environment):
    with pytest.raises(ValueError):
        Tracing.from_environment(environment, release="v1")


def test_agent_address_is_accepted():
    tracing = Tracing.from_environment(
        configured(LANGSMITH_AGENT_ID="condere", LANGSMITH_AGENT_ENVIRONMENT="development"),
        release="v1",
    )
    assert tracing.destination["address"]["environment"] == "DEVELOPMENT"


def test_worker_runs_share_one_trace_with_assessment_metadata(sent):
    tracing = Tracing.from_environment(configured(LANGSMITH_PROJECT="condere-dev"), release="v7")
    model = GenericFakeChatModel(messages=iter([AIMessage("done")]))
    with tracing.worker(
        "condere.technical",
        inputs={"question_id": "T2", "brief": {"original_input": "Assess sqlite-vec"}},
        metadata={"assessment_id": "case-1", "role": "technical"},
        tags=["condere", "technical"],
    ) as handle:
        model.invoke("inspect")
        handle.outputs({"question:T2": {"conclusion": "Embedded extension."}})
    tracing.close()
    payload = b"".join(sent)
    trace_id = handle.trace_id
    assert trace_id and payload.count(trace_id.encode()) >= 2
    assert b"Assess sqlite-vec" in payload and b"Embedded extension." in payload
    assert b"condere.technical" in payload and b"GenericFakeChatModel" in payload
    compact = payload.replace(b" ", b"")
    assert b'"assessment_id":"case-1"' in compact and b'"method_version":"v7"' in compact


def test_work_continues_when_tracing_cannot_start(monkeypatch, sent):
    def unavailable(*args, **kwargs):
        raise RuntimeError("tracing unavailable")

    monkeypatch.setattr(tracing_module, "trace", unavailable)
    tracing = Tracing.from_environment(configured(LANGSMITH_PROJECT="condere-dev"), release="v1")
    finished = False
    with tracing.worker("condere.market", inputs={}, metadata={}, tags=[]) as handle:
        handle.outputs({"ignored": True})
        finished = True
    assert finished and handle.trace_id is None


def test_status_distinguishes_pending_from_unavailable(monkeypatch, sent):
    tracing = Tracing.from_environment(configured(LANGSMITH_PROJECT="condere-dev"), release="v1")

    def missing(client, run_id):
        raise LangSmithNotFoundError("not yet ingested")

    monkeypatch.setattr(type(tracing.client), "read_run", missing)
    assert tracing.status("trace") == ("pending", None)

    def broken(client, run_id):
        raise ConnectionError("offline")

    monkeypatch.setattr(type(tracing.client), "read_run", broken)
    assert tracing.status("trace") == ("unavailable", "ConnectionError")


def test_accepted_answers_become_candidate_examples_linked_to_their_trace(sent, monkeypatch):
    tracing = Tracing.from_environment(
        configured(LANGSMITH_PROJECT="condere-dev", LANGSMITH_DATASET="condere-candidates"),
        release="v9",
    )
    with tracing.worker(
        "condere.technical",
        inputs={"question_id": "T5"},
        metadata={"question_id": "T5"},
        tags=[],
    ) as handle:
        handle.outputs({"question:T5": {"conclusion": "Backups are the burden."}})
    tracing.close()
    calls = []
    monkeypatch.setattr(
        type(tracing.client), "create_example", lambda client, **kwargs: calls.append(kwargs)
    )
    tracing.record_example(handle, {"question:T5": {"conclusion": "Backups are the burden."}})
    [example] = calls
    assert example["dataset_name"] == "condere-candidates"
    assert example["inputs"] == {"question_id": "T5"}
    assert example["source_run_id"] == handle.run.id
    assert example["metadata"]["method_version"] == "v9"
