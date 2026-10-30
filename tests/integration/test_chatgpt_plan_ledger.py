"""ChatGPT plan inference through the real ledger and SDK stream parsing; traffic simulated."""

import json
import os

import httpx
import pytest
from langchain_core.messages import HumanMessage
from test_providers import provider_case as provider_case

from condere.runtime.chatgpt_plan import PLAN_SCOPE, PlanCredentials, PlanUsageStopped, plan_model
from condere.runtime.operations import ProviderRefused

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


def sse(*events):
    body = "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def completed():
    response = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6.1-sol",
        "status": "completed",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_assessment",
                "namespace": "condere",
                "arguments": "{}",
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return {"type": "response.completed", "sequence_number": 1, "response": response}


def credentials(tmp_path):
    saved = PlanCredentials(tmp_path / "chatgpt")
    saved.save(
        {
            "client_id": "oaiapp_fixture",
            "subject": "user-1",
            "access_token": "plan-access",
            "refresh_token": "plan-refresh",
            "expires_at": 4_000_000_000,
            "scopes": ["openid", "offline_access", "resource.invoke", PLAN_SCOPE],
        }
    )
    return saved


def test_plan_inference_is_recorded_without_dollar_liability(provider_case, tmp_path):
    context, _ = provider_case
    sent = []

    def respond(request):
        sent.append(request)
        return sse(completed())

    model = plan_model(
        context,
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    message = model.invoke([HumanMessage("Read the assessment.")])
    assert message.tool_calls[0]["name"] == "read_assessment"
    request = sent[0]
    assert request.url == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer plan-access"
    body = json.loads(request.content)
    assert body["store"] is False and body["stream"] is True and "max_output_tokens" not in body
    [operation] = context.state.operations(context.owner, context.assessment_id)
    assert operation["kind"] == "chatgpt_plan" and operation["state"] == "succeeded"
    assert operation["reserved_microusd"] == 0 and operation["actual_microusd"] == 0
    assert operation["cost_basis"]["usage"]["total_tokens"] == 16
    assert "plan-access" not in json.dumps(operation["request"])
    assert model.invoke([HumanMessage("Read the assessment.")]).tool_calls == message.tool_calls
    assert len(sent) == 1  # The saved answer is reused, not bought again.


def test_usage_limit_stops_work_and_explicit_continuation_retries(provider_case, tmp_path):
    context, _ = provider_case
    replies = [
        httpx.Response(
            429,
            json={
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "Usage limit reached",
                }
            },
        ),
        sse(completed()),
    ]
    model = plan_model(
        context,
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0))),
    )
    with pytest.raises(PlanUsageStopped, match="chatgpt.com/settings/usage"):
        model.invoke([HumanMessage("Read the assessment.")])
    [failed] = context.state.operations(context.owner, context.assessment_id)
    assert failed["state"] == "failed" and failed["result"]["retryable"] is True
    assert failed["actual_microusd"] == 0
    state, owner, case = context.state, context.owner, context.assessment_id
    assert state.get(owner, case)["status"] == "running"
    state.stop(owner, case, generation=context.generation)
    resumed = state.begin(owner, case, resume=True)  # No uncertain charge blocks continuation.
    later = plan_model(
        type(context)(state, owner, case, resumed["generation"], "lead"),
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0))),
    )
    assert later.invoke([HumanMessage("Read the assessment.")]).tool_calls
    keys = [item["operation_key"] for item in state.operations(owner, case)]
    assert keys[1] == keys[0] + ":retry-1"


def test_uncapped_campaign_records_plan_usage(provider_case, tmp_path):
    """Regression: an uncapped campaign must not break saving a provider result."""
    from uuid import uuid4

    from condere.runtime.providers import ProviderContext

    state = provider_case[0].state
    owner, campaign = "uncapped-test-" + str(uuid4()), str(uuid4())
    state.authorize_campaign(owner, campaign, None, {"basis": "uncapped regression fixture"})
    admitted = state.admit(
        owner, campaign, "intake", {"original": "x"}, policy_version="t", method_version="t"
    )
    case = state.begin(owner, admitted["id"])
    context = ProviderContext(state, owner, case["id"], case["generation"], "lead")
    model = plan_model(
        context,
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: sse(completed()))),
    )
    assert model.invoke([HumanMessage("Read the assessment.")]).tool_calls
    [operation] = state.operations(owner, case["id"])
    assert operation["state"] == "succeeded"
    assert state.spending(owner, case["id"])["budget"]["available_microusd"] is None


def test_transient_overload_is_retried_with_each_attempt_recorded(
    provider_case, tmp_path, monkeypatch
):
    import condere.runtime.chatgpt_plan as plan

    monkeypatch.setattr(plan, "TRANSIENT_BACKOFF_SECONDS", (0, 0, 0))
    context, _ = provider_case
    replies = [
        httpx.Response(503, json={"error": {"code": "server_is_overloaded", "message": "busy"}}),
        sse(completed()),
    ]
    model = plan_model(
        context,
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0))),
    )
    assert model.invoke([HumanMessage("Read the assessment.")]).tool_calls
    first, second = context.state.operations(context.owner, context.assessment_id)
    assert first["state"] == "failed" and second["state"] == "succeeded"
    assert second["operation_key"] == first["operation_key"] + ":retry-1"


def test_items_delivered_only_as_stream_events_are_kept(provider_case, tmp_path):
    context, _ = provider_case
    event = completed()
    item = event["response"]["output"][0]
    event["response"]["output"] = []
    added = {"type": "response.output_item.done", "sequence_number": 0, "output_index": 0}

    def respond(request):
        return sse(added | {"item": item}, event)

    model = plan_model(
        context,
        credentials(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    message = model.invoke([HumanMessage("Read the assessment.")])
    assert [call["name"] for call in message.tool_calls] == ["read_assessment"]


def test_a_refused_request_ends_that_call_not_the_assessment(provider_case, tmp_path):
    context, _ = provider_case
    incomplete = {
        "id": "resp_long",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-astra",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_messages"},
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [],
    }
    model = plan_model(
        context,
        credentials(tmp_path),
        model="gpt-6-astra",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: sse({"type": "response.incomplete", "response": incomplete})
            )
        ),
    )
    with pytest.raises(ProviderRefused, match="max_messages"):
        model.invoke([HumanMessage("Read the code.")])
    [failed] = context.state.operations(context.owner, context.assessment_id)
    assert failed["state"] == "failed" and failed["result"]["retryable"] is False
    assert context.state.get(context.owner, context.assessment_id)["status"] == "running"
