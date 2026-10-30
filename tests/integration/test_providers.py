"""Real PostgreSQL and real SDK serialization, with all provider traffic simulated."""

import json
import os
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from openai import APITimeoutError, InternalServerError
from test_evidence import SimulatedObjects

from condere.runtime.evidence import EvidenceError, EvidenceStore
from condere.runtime.providers import (
    CLAUDE_HAIKU,
    CLAUDE_OPUS,
    MODEL_ID,
    MODEL_OUTPUT_TOKENS,
    ApifyResearch,
    ExaResearch,
    JevDecisions,
    ProviderContext,
    accounted_model,
    exa_bound,
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
    usage = {
        "input_tokens": 11,
        "output_tokens": 5,
        "total_tokens": 16,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    if cost is not None:
        usage.update(cost=cost, is_byok=byok)
    return {
        "id": "resp-simulation",
        "object": "response",
        "created_at": 1792540800,
        "model": MODEL_ID,
        "status": "completed",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "id": "fc-simulation",
                "call_id": "call-saved-identity",
                "type": "function_call",
                "name": "record",
                "arguments": '{"finding":"unknown"}',
                "status": "completed",
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
        assert request.url == "https://openrouter.ai/api/v1/responses"
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
        await model.ainvoke(
            [
                HumanMessage("Investigate public alternatives."),
                first,
                ToolMessage("Saved finding.", tool_call_id="call-saved-identity"),
            ]
        )
    assert len(requests) == 3
    assert any(item["type"] == "function_call_output" for item in requests[-1]["input"])
    sent = requests[0]
    assert sent["model"] == MODEL_ID
    assert sent["max_output_tokens"] == MODEL_OUTPUT_TOKENS
    assert "max_completion_tokens" not in sent
    assert sent["stream"] is False
    assert sent["plugins"] == []
    assert sent["provider"]["only"] == ["openai"]
    assert sent["provider"]["allow_fallbacks"] is False
    assert sent["provider"]["require_parameters"] is True
    assert sent["provider"]["max_price"] == {
        "prompt": 4,
        "completion": 15,
        "image": 0,
        "request": 0,
    }
    assert first.tool_calls == repeated.tool_calls
    assert repeated.tool_calls[0]["id"] == "call-saved-identity"
    assert repeated.usage_metadata["total_tokens"] == 0
    assert repeated.response_metadata["condere"]["new_cost_microusd"] == 0
    assert repeated.response_metadata["condere"]["reused"] is True
    saved = operations(context)
    assert len(saved) == 3
    assert all(item["actual_microusd"] == 31 for item in saved)
    assert all(item["reserved_microusd"] == 5_372_880 for item in saved)
    assert "not an invoice" in saved[0]["cost_basis"]["basis"]


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": MODEL_OUTPUT_TOKENS + 1},
        {"max_completion_tokens": MODEL_OUTPUT_TOKENS + 1},
        {"max_output_tokens": MODEL_OUTPUT_TOKENS + 1},
        {"store": True},
        {"previous_response_id": "resp-unrecorded-input"},
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
        == 5_372_880
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
                "numResults": 10,
                "contents": {
                    "highlights": {
                        "query": "public technology alternatives",
                        "maxCharacters": 4_000,
                    }
                },
            },
        ),
        (
            "/contents",
            {
                "urls": ["https://example.com/fictional-evidence"],
                "text": {"maxCharacters": 12_000},
                "maxAgeHours": 0,
                "livecrawlTimeout": 15_000,
                "extras": {"links": 50},
            },
        ),
    ]
    source = evidence.source(context.owner, context.assessment_id, result["source_ids"][0])
    assert source.provenance["request_id"] == "exa-simulation"
    assert source.provenance["result_index"] == 0
    assert "not a publisher HTML" in source.limitations[0]
    spending = context.state.spending(context.owner, context.assessment_id)
    # Ten pages of highlights on an auto search (17,000) plus one live page fetch (3,000).
    assert spending["budget"]["pending_microusd"] == 20_000
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
        for count in (0, 26, True):
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


def test_claude_specs_route_only_to_anthropic_within_their_price_bounds(provider_case):
    context, _ = provider_case
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion() | {"model": CLAUDE_HAIKU.id})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        for spec in (CLAUDE_OPUS, CLAUDE_HAIKU):
            accounted_model(context, "fake-router-key", spec=spec, http_client=client).invoke(
                "Summarize the saved source."
            )
        with pytest.raises(ValueError, match="output-token cap"):
            accounted_model(
                context, "fake-router-key", spec=CLAUDE_HAIKU, http_client=client
            ).invoke("Too long.", max_output_tokens=CLAUDE_HAIKU.output_tokens + 1)
    opus, haiku = requests
    assert opus["model"] == CLAUDE_OPUS.id and haiku["model"] == CLAUDE_HAIKU.id
    assert opus["max_output_tokens"] == CLAUDE_OPUS.output_tokens
    assert opus["provider"]["only"] == ["anthropic"]
    assert opus["provider"]["max_price"] == {
        "prompt": 4,
        "completion": 20,
        "image": 0,
        "request": 0,
    }
    assert haiku["provider"]["max_price"]["completion"] == 0.5
    reserved = sorted(item["reserved_microusd"] for item in operations(context))
    # Haiku's full-context bound is far below Opus's; each is reserved before dispatch.
    assert reserved[0] < 200_000 < 5_000_000 < reserved[1]


def jev_answer(questions):
    return {
        "model": "typesafe/jev-1.14-20261014",
        "answers": {
            name: {"type": "choice", "choice": "O1", "probabilities": {"O1": 1}, "confidence": 1}
            for name in questions
        },
        "usage": {"input_tokens": 400, "output_tokens": 20, "cost": 0.0000168},
    }


def test_jev_answers_once_per_request_and_records_its_reported_cost(provider_case):
    context, _ = provider_case
    requests = []
    questions = {"option": {"type": "choice", "instructions": "Which?", "criteria": {"O1": "a"}}}

    def respond(request):
        assert request.url == "https://openrouter.ai/api/v1/systemone"
        assert request.headers["authorization"] == "Bearer fake-router-key"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=jev_answer(requests[-1]["questions"]))

    jev = JevDecisions(context, "fake-router-key", transport=httpx.MockTransport(respond))
    first = jev.ask(["Fictional brief."], questions, "choose option")
    again = jev.ask(["Fictional brief."], questions, "choose option")
    assert len(requests) == 1 and requests[0]["model"] == "typesafe/jev-1.14"
    assert first["answers"]["option"]["choice"] == "O1"
    assert again["operation_id"] == first["operation_id"]
    [operation] = operations(context)
    assert operation["kind"] == "jev" and operation["actual_microusd"] == 17
    # The bound covers Jev's billed prompt overhead, which exceeded a bytes-only bound live.
    assert operation["reserved_microusd"] >= operation["actual_microusd"]
    with pytest.raises(ValueError, match="under 32,000 bytes"):
        jev.ask(["x" * 32_001], questions, "choose option")
    with pytest.raises(ValueError, match="choice, score or noul"):
        jev.ask(["Brief."], {"box": {"type": "box", "instructions": "x"}}, "choose option")


def test_jev_rejects_answers_to_other_questions(provider_case):
    context, _ = provider_case

    def respond(request):
        return httpx.Response(200, json=jev_answer({"other": {}}))

    jev = JevDecisions(context, "fake-router-key", transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError, match="exactly the questions asked"):
        jev.ask(["Brief."], {"option": {"type": "choice", "instructions": "Which?"}}, "x")


def test_exa_search_options_reach_the_request_and_raise_the_bound(provider_case):
    from datetime import date

    context, evidence = provider_case
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        result = research_response()["results"][0] | {
            "text": None,
            "highlights": ["Operators report upgrades breaking in 2026."],
            "extras": {"links": ["https://example.com/follow-up", 7]},
        }
        return httpx.Response(200, json=research_response() | {"results": [result]})

    exa = ExaResearch(context, evidence, "fake-exa-key", transport=httpx.MockTransport(respond))
    try:
        found = exa.search(
            "vector extension upgrade pain",
            "Need current operator complaints",
            num_results=20,
            depth="deep",
            additional_queries=("sqlite vector upgrade failure",),
            published_after=date(2026, 7, 1),
            category="news",
            include_domains=("reddit.com", "news.ycombinator.com"),
            fresh=True,
        )
        for bad in (
            {"category": "company", "published_after": date(2026, 1, 1)},
            {"additional_queries": ("other phrasing",)},
            {"include_domains": ("http://private",)},
            {"depth": "unbounded"},
        ):
            with pytest.raises(ValueError):
                exa.search("vector extension", "Need evidence", **bad)
    finally:
        exa.close()
    [sent] = requests
    assert sent["type"] == "deep" and sent["numResults"] == 20
    assert sent["additionalQueries"] == ["sqlite vector upgrade failure"]
    assert sent["startPublishedDate"] == "2026-07-01T00:00:00.000Z"
    assert sent["category"] == "news"
    assert sent["includeDomains"] == ["reddit.com", "news.ycombinator.com"]
    assert sent["contents"]["maxAgeHours"] == 0
    [operation] = operations(context)
    assert operation["reserved_microusd"] == exa_bound("deep", 20, True, 1) == 89_000
    assert found["results"][0]["published_at"] == "2026-10-01"
    source = evidence.source(context.owner, context.assessment_id, found["source_ids"][0])
    assert evidence.text(context.owner, context.assessment_id, source.id).startswith("Operators")
    assert source.provenance["links"] == ["https://example.com/follow-up"]
    assert source.provenance["live_fetch"] is True
    assert any("highlights only" in item for item in source.limitations)


TWEET = {
    "id": "2115777960355708930",
    "url": "https://x.com/example/status/2115777960355708930",
    "createdAt": "Thu Oct 29 12:09:09 +0000 2026",
    "authorUsername": "example",
    "text": "Data is stored locally in sqlite-vec but it also leaves the system.",
    "conversationId": "2115760778951409799",
    "likeCount": 1,
    "viewCount": 77,
    "entities": {"urls": [{"expandedUrl": "https://example.com/announcement"}]},
}
REDDIT_POST = {
    "id": "t3_1xgtdb5",
    "dataType": "post",
    "title": "SQLite editor with sqlite_vec",
    "body": "",
    "postUrl": "https://www.reddit.com/r/sqlite/comments/1xgtdb5/editor/",
    "contentUrl": "https://github.com/example/editor",
    "outboundUrlHost": "github.com",
    "createdAt": "2026-10-27T14:01:49.000Z",
    "communityName": "r/sqlite",
    "upVotes": 2,
    "commentsCount": 0,
}
VIDEO = {
    "video_id": "1UL8iZJD38U",
    "url": "https://www.youtube.com/watch?v=1UL8iZJD38U",
    "title": "Fictional demo",
    "channel_name": "Example",
    "text": "In this demo we run vector search inside SQLite.",
    "description": "Docs: https://example.com/docs",
}


def test_apify_runs_are_capped_upstream_and_saved_with_origin_groups(provider_case):
    from decimal import Decimal

    context, evidence = provider_case
    requests = []
    replies = {"xquik": [TWEET], "harshmaur": [REDDIT_POST], "johnvc": [VIDEO]}

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fake-apify-token"
        owner = request.url.path.split("/acts/")[1].split("~")[0]
        return httpx.Response(201, json=replies[owner])

    apify = ApifyResearch(
        context, evidence, "fake-apify-token", transport=httpx.MockTransport(respond)
    )
    try:
        tweets = apify.x_posts(
            ("https://x.com/example/status/2115777960355708930",), "Need the post"
        )
        threads = apify.reddit("Need user pain", query="sqlite vector extension", max_posts=5)
        videos = apify.youtube_transcripts(("https://youtu.be/1UL8iZJD38U",), "Need the demo")
        for call in (
            lambda: apify.x_posts(("not-a-tweet",), "Need the post"),
            lambda: apify.reddit("Need pain", thread_urls=("https://example.com/r/x",)),
            lambda: apify.reddit("Need pain"),
            lambda: apify.youtube_transcripts(("https://vimeo.com/1",), "Need demo"),
        ):
            with pytest.raises(ValueError):
                call()
    finally:
        apify.close()
    assert [request.url.path for request in requests] == [
        "/v2/acts/xquik~x-tweet-scraper/run-sync-get-dataset-items",
        "/v2/acts/harshmaur~reddit-scraper/run-sync-get-dataset-items",
        "/v2/acts/johnvc~YoutubeTranscripts/run-sync-get-dataset-items",
    ]
    assert json.loads(requests[0].content)["tweetIds"] == ["2115777960355708930"]
    # One tweet costs far below the minimum cap that keeps the Actor from returning nothing.
    assert Decimal(requests[0].url.params["maxTotalChargeUsd"]) == Decimal("0.01")
    # Five posts with ten comments each is 55 results: (0.02 + 55 * 0.002) * 1.25.
    assert Decimal(requests[1].url.params["maxTotalChargeUsd"]) == Decimal("0.1625")
    tweet = tweets["results"][0]
    assert tweet["published_at"] == "2026-10-29T12:09:09+00:00"
    assert tweet["origin_group"] == "x:conversation:2115760778951409799"
    source = evidence.source(context.owner, context.assessment_id, tweet["source_id"])
    assert source.provenance["links"] == ["https://example.com/announcement"]
    assert "@example on X" in evidence.text(context.owner, context.assessment_id, source.id)
    assert threads["results"][0]["origin_group"] == "reddit:t3_1xgtdb5"
    reddit = evidence.source(context.owner, context.assessment_id, threads["source_ids"][0])
    assert reddit.provenance["links"] == ["https://github.com/example/editor"]
    video = evidence.source(context.owner, context.assessment_id, videos["source_ids"][0])
    assert video.provenance["links"] == ["https://example.com/docs"]
    assert any("Upload date" in item for item in video.limitations)
    kinds = [item["kind"] for item in operations(context)]
    assert kinds == ["apify", "apify", "apify"]
    pending = context.state.spending(context.owner, context.assessment_id)["budget"]
    assert pending["pending_microusd"] == sum(
        item["reserved_microusd"] for item in operations(context)
    )
