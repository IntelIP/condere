"""Real PostgreSQL and real SDK serialization, with all provider traffic simulated."""

import json
import os
from uuid import uuid4

import httpx
import pytest
from openai import APITimeoutError, InternalServerError
from test_evidence import SimulatedObjects

from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.providers import (
    MODEL_ID,
    MODEL_OUTPUT_TOKENS,
    ExaResearch,
    ProviderContext,
    accounted_model,
    public_query,
    public_url,
)
from condere.runtime.state import AssessmentStore, Conflict, RecoveryRequired

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


@pytest.fixture
def provider_case(monkeypatch):
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "EXA_API_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def forbid_network(*args, **kwargs):
        pytest.fail("Provider tests must use MockTransport; real HTTP is forbidden.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid_network)
    state = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    state.setup()
    owner, campaign = "provider-test-" + str(uuid4()), str(uuid4())
    state.authorize_campaign(owner, campaign, 10_000_000, {"basis": "simulation; no spending"})
    admitted = state.admit(
        owner,
        campaign,
        "intake",
        {"original": "Fictional technology"},
        policy_version="test",
        method_version="test",
    )
    case = state.begin(owner, admitted["id"])
    context = ProviderContext(state, owner, case["id"], case["generation"], "technical")
    return context, EvidenceStore(state, SimulatedObjects(), "condere-provider-test")


def completion(*, cost=0.00003075, byok=False):
    usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
    if cost is not None:
        usage.update(cost=cost, is_byok=byok)
    return {
        "id": "chatcmpl-simulation",
        "object": "chat.completion",
        "created": 1792540800,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-saved-identity",
                            "type": "function",
                            "function": {"name": "record", "arguments": '{"finding":"unknown"}'},
                        }
                    ],
                },
            }
        ],
        "usage": usage,
    }


def operations(context):
    return context.state.operations(context.owner, context.assessment_id)


@pytest.mark.asyncio
async def test_model_sdk_limits_saved_tool_ids_and_async_accounting(provider_case):
    context, _ = provider_case
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-router-key"
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = accounted_model(context, "fake-router-key", http_client=client).bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "record",
                        "description": "Record a finding.",
                        "parameters": {
                            "type": "object",
                            "properties": {"finding": {"type": "string"}},
                        },
                    },
                }
            ]
        )
        first = model.invoke("Investigate public alternatives.")
        repeated = await model.ainvoke("Investigate public alternatives.")
        await model.ainvoke("Investigate another public alternative.")
    assert len(requests) == 2
    sent = requests[0]
    assert sent["model"] == MODEL_ID
    assert sent["max_tokens"] == MODEL_OUTPUT_TOKENS
    assert "max_completion_tokens" not in sent
    assert sent["stream"] is False
    assert sent["plugins"] == []
    assert sent["provider"]["only"] == ["openai"]
    assert sent["provider"]["allow_fallbacks"] is False
    assert sent["provider"]["require_parameters"] is True
    assert sent["provider"]["max_price"] == {
        "prompt": 0.75,
        "completion": 4.5,
        "image": 0,
        "request": 0,
    }
    assert first.tool_calls == repeated.tool_calls
    assert repeated.tool_calls[0]["id"] == "call-saved-identity"
    assert repeated.usage_metadata["total_tokens"] == 0
    assert repeated.response_metadata["condere"]["new_cost_microusd"] == 0
    assert repeated.response_metadata["condere"]["reused"] is True
    saved = operations(context)
    assert len(saved) == 2
    assert all(item["actual_microusd"] == 31 for item in saved)
    assert all(item["reserved_microusd"] == 336_864 for item in saved)
    assert "not an invoice" in saved[0]["cost_basis"]["basis"]


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": MODEL_OUTPUT_TOKENS + 1},
        {"max_completion_tokens": MODEL_OUTPUT_TOKENS + 1},
        {"extra_body": {"plugins": [{"id": "web"}]}},
        {"tools": [{"type": "web_search"}]},
        {"n": 2},
        {"model": "another/model"},
    ],
)
def test_unbounded_model_options_fail_before_reservation_or_dispatch(provider_case, options):
    context, _ = provider_case
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))) as c:
        model = accounted_model(context, "fake-router-key", http_client=c)
        with pytest.raises(ValueError):
            model.invoke("Public alternatives", **options)
    assert operations(context) == []


@pytest.mark.parametrize("cost,byok", [(None, False), (0.01, True)])
def test_model_missing_or_byok_cost_keeps_reservation_pending(provider_case, cost, byok):
    context, _ = provider_case
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion(cost=cost, byok=byok))
        )
    ) as client:
        accounted_model(context, "fake-router-key", http_client=client).invoke(
            "Public alternatives"
        )
    saved = operations(context)[0]
    assert saved["state"] == "succeeded"
    assert saved["actual_microusd"] is None
    assert (
        context.state.spending(context.owner, context.assessment_id)["budget"]["pending_microusd"]
        == 336_864
    )


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_model_failure_keeps_liability_and_never_retries(provider_case, failure):
    context, _ = provider_case
    calls = []

    def timeout(request):
        calls.append(request)
        if failure == "server_error":
            return httpx.Response(503, json={"error": {"message": "Simulated provider outage"}})
        raise httpx.ReadTimeout("Simulated unknown provider outcome", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        model = accounted_model(context, "fake-router-key", http_client=client)
        with pytest.raises(APITimeoutError if failure == "timeout" else InternalServerError):
            model.invoke("Public alternatives")
        with pytest.raises((Conflict, RecoveryRequired)):
            model.invoke("Public alternatives")
    assert len(calls) == 1
    saved = operations(context)[0]
    assert saved["actual_microusd"] is None
    assert saved["state"] == "uncertain"
    assert context.state.get(context.owner, context.assessment_id)["status"] == "interrupted"


def research_response(cost=0.007):
    return {
        "requestId": "exa-simulation",
        "costDollars": {"total": cost},
        "results": [
            {
                "url": "https://example.com/fictional-evidence",
                "title": "Fictional source",
                "text": "Fictional evidence for a contract check.",
                "publishedDate": "2026-10-01",
            }
        ],
        "notices": ["Simulated provider response; no market evidence."],
    }


def test_exa_bounds_reuse_sources_and_keep_estimates_pending(provider_case):
    context, evidence = provider_case
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        assert request.headers["x-api-key"] == "fake-exa-key"
        return httpx.Response(200, json=research_response(0.001 if len(requests) > 1 else 0.007))

    exa = ExaResearch(context, evidence, "fake-exa-key", transport=httpx.MockTransport(respond))
    try:
        result = exa.search("public technology alternatives", "Missing comparable technologies")
        again = exa.search("public technology alternatives", "A different explanation of that gap")
        assert result == again
        assert len(requests) == 1
        exa.contents(result["source_ids"][0], "Need the original extracted passage")
    finally:
        exa.close()
    assert requests == [
        (
            "/search",
            {
                "query": "public technology alternatives",
                "type": "auto",
                "numResults": 5,
                "contents": {"text": {"maxCharacters": 12_000}},
            },
        ),
        (
            "/contents",
            {"urls": ["https://example.com/fictional-evidence"], "text": {"maxCharacters": 12_000}},
        ),
    ]
    source = evidence.source(context.owner, context.assessment_id, result["source_ids"][0])
    assert source.provenance["request_id"] == "exa-simulation"
    assert source.provenance["result_index"] == 0
    assert "not a publisher HTML" in source.limitations[0]
    spending = context.state.spending(context.owner, context.assessment_id)
    assert spending["budget"]["pending_microusd"] == 8_000
    assert spending["budget"]["settled_microusd"] == 0
    assert all(item["actual_microusd"] is None for item in spending["operations"])
    assert (
        "estimate, not an invoice"
        in spending["operations"][0]["reconciliation"]["pending_cost_basis"]["basis"]
    )


def test_exa_storage_repair_does_not_buy_research_again(provider_case):
    context, evidence = provider_case
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=research_response())

    evidence.client.fail_at = 1
    exa = ExaResearch(context, evidence, "fake-exa-key", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(EvidenceError, match="could not be saved"):
            exa.search("public alternatives", "Missing comparator")
        assert operations(context)[0]["state"] == "succeeded"
        evidence.client.fail_at = None
        recovered = exa.search("public alternatives", "Missing comparator")
    finally:
        exa.close()
    assert len(requests) == 1
    assert len(recovered["source_ids"]) == 1


def test_exa_unexpected_estimate_increases_liability_and_interrupts(provider_case):
    context, evidence = provider_case
    exa = ExaResearch(
        context,
        evidence,
        "fake-exa-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=research_response(0.02))),
    )
    try:
        exa.search("public alternatives", "Missing comparator")
    finally:
        exa.close()
    spending = context.state.spending(context.owner, context.assessment_id)
    assert spending["budget"]["pending_microusd"] == 20_000
    assert context.state.get(context.owner, context.assessment_id)["status"] == "interrupted"
    operation = operations(context)[0]
    context.state.record_pending_cost(
        context.owner, context.assessment_id, operation["id"], 1_000, {"basis": "lower estimate"}
    )
    assert operations(context)[0]["reserved_microusd"] == 20_000


def test_private_inputs_and_unbounded_searches_fail_before_http(provider_case):
    context, evidence = provider_case
    exa = ExaResearch(
        context,
        evidence,
        "fake-exa-key",
        private_terms=("Private Project",),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
    )
    try:
        for query in (
            "Private Project alternatives",
            "person@example.com",
            "token=secret-value",
            "https://private.example.com",
            "+1 212 555 0101",
            "fake-exa-key",
        ):
            with pytest.raises(ValueError):
                exa.search(query, "Missing information")
        for count in (0, 11, True):
            with pytest.raises(ValueError):
                exa.search("public alternatives", "Missing information", num_results=count)
        with pytest.raises(ValueError):
            exa.search("public alternatives", "")
    finally:
        exa.close()
    for url in (
        "http://127.0.0.1",
        "http://localhost",
        "http://10.0.0.1",
        "http://[::1]",
        "https://a:b@example.com",
        "https://example.com:9000",
        "https://example.com?token=x",
    ):
        with pytest.raises(ValueError):
            public_url(url)
    assert public_query("public alternatives 2026-10-21") == "public alternatives 2026-10-21"
    assert operations(context) == []


def test_external_images_and_native_file_inputs_fail_before_model_dispatch(provider_case):
    context, _ = provider_case
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))) as c:
        model = accounted_model(context, "fake-router-key", http_client=c)
        for block in (
            {"type": "image_url", "image_url": {"url": "https://example.com/private-diagram.png"}},
            {"type": "file", "file": {"file_id": "file-external"}},
        ):
            with pytest.raises(ValueError):
                model.invoke([{"role": "user", "content": [block]}])
    assert operations(context) == []
