"""Real SmolVM, Deep Agents, PostgreSQL and checkpoints with a labelled deterministic model."""

import json
import os

import pytest
from langchain_core.messages import AIMessage
from test_sandbox import RehearsalModel
from test_sandbox import sandboxes as sandboxes
from test_service import question
from test_service import setup_case as setup_case

from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.state import Conflict
from condere.runtime.workers import run_worker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1"
        or os.environ.get("CONDERE_TEST_SANDBOX") != "1",
        reason="Explicit PostgreSQL and native sandbox opt-in required",
    ),
]


@pytest.mark.asyncio
async def test_assigned_worker_saves_all_answers_and_reuses_completed_checkpoint(
    setup_case, sandboxes, tmp_path, monkeypatch
):
    service, owner, case = setup_case
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    model = RehearsalModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}]
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_question",
                        "args": {"payload": question("M" + str(index))},
                        "id": "submit-" + str(index),
                    }
                    for index in range(1, 9)
                ],
            ),
            AIMessage(content="All eight fictional Market records have been submitted."),
        ]
    )
    args = dict(
        service=service,
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        role="market",
        sandbox=sandboxes[0],
        model=model,
        research=None,
        repository_cache=tmp_path,
        allowed_repositories=set(),
    )
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        completed = await run_worker(**args, checkpointer=checkpointer)
        assert completed["status"] == "completed"
        assert completed["machine_id"] == sandboxes[0].id
        saved = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": completed["thread_id"]}}
        )
        assert saved is not None
    calls = len(model.seen_system)
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as reopened:
        assert await run_worker(**args, checkpointer=reopened) == completed
    assert len(model.seen_system) == calls
    assert "submit_question" in model.tool_names
    assert "submit_technical" not in model.tool_names
    assert "submit_review" not in model.tool_names
    assert "task" not in model.tool_names
    exported = sandboxes[0].download_files(["/workspace/results/market.json"])[0]
    assert exported.error is None
    artifact = json.loads(exported.content)
    records = service.state.records(owner, case["id"], kind="question")
    assert set(artifact) == {"question:M" + str(index) for index in range(1, 9)}
    for record in records:
        assert (
            artifact["question:" + record["record_key"]]["content_hash"] == record["content_hash"]
        )
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_worker_cannot_finish_without_accepted_findings(setup_case, sandboxes, tmp_path):
    service, owner, case = setup_case
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        with pytest.raises(Conflict, match="without submitting"):
            await run_worker(
                service,
                owner=owner,
                assessment_id=case["id"],
                generation=case["generation"],
                role="business",
                sandbox=sandboxes[1],
                model=RehearsalModel(
                    responses=[AIMessage(content="Done, with no accepted records.")]
                ),
                checkpointer=checkpointer,
                research=None,
                repository_cache=tmp_path,
                allowed_repositories=set(),
            )
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    assert service.state.records(owner, case["id"], kind="question") == []
