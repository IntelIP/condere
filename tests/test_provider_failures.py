"""A failed provider call reaches the agent as a message; only a lead model failure ends a run."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from condere.runtime.agent import ProviderFailures
from condere.runtime.operations import ProviderRefused, ProviderUncertain
from condere.runtime.providers import _read_again

REQUEST = SimpleNamespace(tool_call={"id": "call-1", "name": "reddit_research"})


def failing(error):
    def handler(request):
        raise error

    return handler


def test_tool_failures_become_error_messages():
    middleware = ProviderFailures(end_task_on_model_failure=False)
    message = middleware.wrap_tool_call(
        REQUEST, failing(ProviderRefused("apify", 402, "Monthly usage exceeded"))
    )
    assert message.status == "error" and message.tool_call_id == "call-1"
    assert "402" in message.content and "another route" in message.content


def test_subagent_model_failure_ends_the_task_but_the_lead_failure_propagates():
    held = ProviderUncertain("openrouter call failed and may have billed (timeout)")

    async def handler(request):
        raise held

    ended = asyncio.run(
        ProviderFailures(end_task_on_model_failure=True).awrap_model_call(REQUEST, handler)
    )
    assert "task stopped" in ended.result[0].content
    with pytest.raises(ProviderUncertain):
        asyncio.run(
            ProviderFailures(end_task_on_model_failure=False).awrap_model_call(REQUEST, handler)
        )


def test_other_errors_are_not_hidden():
    with pytest.raises(ValueError):
        ProviderFailures(end_task_on_model_failure=True).wrap_tool_call(
            REQUEST, failing(ValueError("bad input"))
        )


def test_read_only_requests_survive_transient_errors():
    replies = iter([httpx.Response(502), httpx.Response(429), httpx.Response(200, json=[1])])
    request = httpx.Request("GET", "https://api.apify.com/v2/datasets/x/items")

    def read():
        reply = next(replies)
        reply.request = request
        return reply

    assert _read_again(read, pause=0).json() == [1]
    refused = httpx.Response(404, request=request)
    with pytest.raises(httpx.HTTPStatusError):
        _read_again(lambda: refused, pause=0)
