"""Worker tools against real PostgreSQL; providers, object storage, and machines are simulated."""

import base64
import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_providers import research_response
from test_service import accept_findings, question, worker
from test_service import setup_case as setup_case

from condere.runtime.contracts import QuestionAnswer
from condere.runtime.providers import ExaResearch, ProviderContext
from condere.runtime.repositories import RepositorySnapshot
from condere.runtime.state import Conflict
from condere.runtime.tools import worker_tools

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


@pytest.fixture
def tool_case(setup_case, tmp_path, monkeypatch):
    service, owner, case = setup_case
    requests, clients = [], []
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "EXA_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def forbid_http(*args, **kwargs):
        pytest.fail("Worker-tool checks must not make real provider requests.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid_http)

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=research_response())

    def build(role, question_id=None):
        assigned = worker(service, owner, case, role, question_id)
        sandbox = SimpleNamespace(id=assigned["machine_id"])
        research = None
        if role != "review":
            research = ExaResearch(
                ProviderContext(service.state, owner, case["id"], case["generation"], role),
                service.evidence,
                "fake-exa-key",
                transport=httpx.MockTransport(respond),
            )
            clients.append(research)
        tools = worker_tools(
            service,
            owner=owner,
            assessment_id=case["id"],
            generation=case["generation"],
            worker_id=assigned["id"],
            research=research,
            sandbox=sandbox,
            repository_cache=tmp_path,
            allowed_repositories={"https://github.com/example/project"},
        )
        return {item.name: item for item in tools}, assigned, sandbox

    yield service, owner, case, build, requests
    for client in clients:
        client.close()


def test_research_to_citation_and_assigned_submission(tool_case):
    service, owner, case, build, requests = tool_case
    tools, _, _ = build("market", "M1")
    assert "submit_question" in tools
    assert not {"submit_review", "inspect_repository"} & tools.keys()
    context = tools["read_assessment"].invoke({})
    assert context["submission_schema"]["title"] == "QuestionAnswer"
    assert list(context["question_contracts"]) == ["M1"]
    assert context["brief"]["original_input"].startswith("  Assess")
    request = {"query": "public technology alternatives", "material_gap": "Missing comparator"}
    found = tools["search_public"].invoke(request)
    assert tools["search_public"].invoke(request) == found
    assert len(requests) == 1
    sources = tools["list_sources"].invoke({})
    assert sources["total"] == 1
    source_id = found["source_ids"][0]
    window = tools["read_source"].invoke({"source_id": source_id})
    original = tools["read_original"].invoke({"source_id": source_id})
    assert json.loads(base64.b64decode(original["content"]))["requestId"] == "exa-simulation"
    payload = question("M1") | {"source_ids": [source_id], "citations": [window["citation"]]}
    forged = payload | {"citations": [window["citation"] | {"quote": "Invented quotation"}]}
    assert isinstance(tools["submit_question"].invoke({"payload": forged}), str)
    accepted = tools["submit_question"].invoke({"payload": payload})
    assert tools["submit_question"].invoke({"payload": payload}) == accepted
    assert accepted["version"] == 1
    assert "outside" in tools["submit_question"].invoke({"payload": question("B1")})
    assert service.state.records(owner, case["id"], kind="question")[0]["payload"] == (
        QuestionAnswer.model_validate(payload).model_dump(mode="json")
    )
    calls = service.evidence.client.calls
    service.state.stop(owner, case["id"], generation=case["generation"])
    for name, args in (("read_source", {"source_id": source_id}), ("search_public", request)):
        with pytest.raises(Conflict, match="not running"):
            tools[name].invoke(args)
    assert service.evidence.client.calls == calls
    assert len(requests) == 1


def test_reviewer_reads_full_saved_evidence_without_research_tools(tool_case):
    service, owner, case, build, requests = tool_case
    accept_findings(service, owner, case)
    tools, _, _ = build("review")
    assert (
        not {"search_public", "fetch_public_source", "inspect_repository", "submit_question"}
        & tools.keys()
    )
    context = tools["read_assessment"].invoke({})
    assert context["submission_schema"]["title"] == "FinalReview"
    assert len(context["review_context"]["reviewed_hashes"]) == 26
    raw = "Long fictional evidence. " * 1500
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/long-fictional-source",
        original=raw.encode(),
        text=raw,
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["Fictional evidence only."],
    )
    text, offset = "", 0
    while offset is not None:
        result = tools["read_source"].invoke({"source_id": source.id, "start": offset})
        text += result["citation"]["quote"]
        offset = result["next_offset"]
    assert text == raw
    assert requests == []
    assert "Field required" in tools["submit_review"].invoke({"payload": {}})


def test_calculation_preserves_decimals_unknowns_and_receipt_requirements(tool_case):
    service, owner, case, build, _ = tool_case
    tools, _, _ = build("business", "B3")
    calculation = {
        "id": "fictional-cost",
        "scenario": "single",
        "operation": "add",
        "output_unit": "USD",
        "inputs": [
            {
                "name": name,
                "value": value,
                "unit": "USD",
                "basis": "modelled",
                "assumptions": ["Fictional price for a contract test."],
                "as_of": "2026-10-21",
            }
            for name, value in (("first", "0.1"), ("second", "0.2"))
        ],
    }
    result = tools["calculate"].invoke({"calculation": calculation})
    assert result["value"] == "0.3"
    assert tools["calculate"].invoke({"calculation": calculation}) == result
    assert len(service.state.records(owner, case["id"], kind="calculation")) == 1
    missing = calculation | {
        "inputs": [
            {"name": "first", "value": None, "unit": "USD", "limitations": ["Price unknown."]},
            calculation["inputs"][1],
        ]
    }
    unknown = tools["calculate"].invoke({"calculation": missing})
    assert unknown["value"] is None
    assert unknown["unavailable_inputs"] == ["first"]
    found = tools["search_public"].invoke(
        {"query": "public price", "material_gap": "Missing price"}
    )
    measured = calculation | {
        "inputs": [
            calculation["inputs"][0] | {"basis": "measured", "source_ids": found["source_ids"]},
            calculation["inputs"][1],
        ]
    }
    assert "execution receipt" in tools["calculate"].invoke({"calculation": measured})


def test_repository_inspection_pins_commit_and_rejects_changed_guest_files(
    tool_case, tmp_path, monkeypatch
):
    service, owner, case, build, _ = tool_case
    tools, _, sandbox = build("technical", "T2")
    content = b"Fictional repository README."
    archive = tmp_path / "simulated.tar.gz"
    archive.write_bytes(b"Simulated archive; this is not a native repository test.")
    snapshot = RepositorySnapshot(
        "https://github.com/example/project",
        "a" * 40,
        archive,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        (
            {
                "path": "README.md",
                "kind": "file",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        ),
    )
    commits = []

    def acquire(url, **kwargs):
        commits.append(kwargs["commit"])
        return replace(snapshot, repository=url)

    monkeypatch.setattr("condere.runtime.indexing.acquire_repository", acquire)
    monkeypatch.setattr(
        "condere.runtime.tools.stage_repository",
        lambda *_: {
            "path": "/workspace/simulated-repository",
            "skipped_links": [],
        },
    )
    sandbox.download_files = lambda paths: [SimpleNamespace(content=content, error=None)]
    request = {"repository_url": snapshot.repository, "paths": ["README.md"]}
    result = tools["inspect_repository"].invoke(request)
    assert result["inspection"]["inspected_files"] == {
        "README.md": hashlib.sha256(content).hexdigest()
    }
    assert result["inspection"]["commit"] == "a" * 40
    tools["inspect_repository"].invoke(request)
    assert commits == [None, "a" * 40]
    assert "different repository commit" in tools["inspect_repository"].invoke(
        request | {"commit": "b" * 40}
    )
    assert "outside" in tools["inspect_repository"].invoke(
        request | {"repository_url": "https://github.com/other/repo"}
    )
    sandbox.download_files = lambda paths: [
        SimpleNamespace(content=b"Changed guest file", error=None)
    ]
    assert "differs" in tools["inspect_repository"].invoke(request)
    records = service.state.records(owner, case["id"], kind="repository_inspection")
    assert len(records) == 2
    assert all(item["payload"]["machine_id"] == sandbox.id for item in records)
    discovered = "https://github.com/discovered/project"
    service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin=discovered + "/blob/main/README.md",
        original=b"Fictional public research result.",
        text="Fictional public research result.",
        media_type="text/plain",
        provenance={"provider": "exa"},
        limitations=["Simulated source; not real research."],
    )
    sandbox.download_files = lambda paths: [SimpleNamespace(content=content, error=None)]
    discovered_result = tools["inspect_repository"].invoke(request | {"repository_url": discovered})
    assert discovered_result["inspection"]["repository_url"] == discovered
    assert len(service.state.records(owner, case["id"], kind="repository_admission")) == 2
