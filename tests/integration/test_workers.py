"""Real SmolVM, Deep Agents, PostgreSQL and checkpoints with a labelled deterministic model."""

import json
import os

import pytest
from langchain_core.messages import AIMessage
from pydantic import Field
from test_sandbox import RehearsalModel
from test_sandbox import sandboxes as sandboxes
from test_service import question
from test_service import setup_case as setup_case

from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.state import Conflict
from condere.runtime.tools import MAX_REJECTED_SUBMISSIONS, CorrectionLimitReached
from condere.runtime.workers import run_worker


class CorrectingModel(RehearsalModel):
    observed_errors: list[str] = Field(default_factory=list)
    seen_text: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_text.extend(str(message.content) for message in messages)
        self.observed_errors.extend(
            str(message.content)
            for message in messages
            if getattr(message, "status", None) == "error"
        )
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


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
    model = CorrectingModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}]
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_question",
                        "args": {"payload": question("M2")},
                        "id": "wrong-question",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_question",
                        "args": {"payload": question("M1")},
                        "id": "submit-M1",
                    }
                ],
            ),
            AIMessage(content="The fictional M1 record has been submitted."),
        ]
    )
    args = dict(
        service=service,
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        role="market",
        question_id="M1",
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
        assert any("outside" in message for message in model.observed_errors)
    calls = len(model.seen_system)
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as reopened:
        assert await run_worker(**args, checkpointer=reopened) == completed
    assert len(model.seen_system) == calls
    assert "submit_question" in model.tool_names
    assert "submit_review" not in model.tool_names
    assert "inspect_repository" not in model.tool_names
    assert "task" not in model.tool_names
    skills = sandboxes[0].execute("ls /workspace/skills").output.split()
    assert sorted(skills) == ["market-evidence", "opportunity-framing"]
    exported = sandboxes[0].download_files(["/workspace/results/market-M1.json"])[0]
    assert exported.error is None
    artifact = json.loads(exported.content)
    records = service.state.records(owner, case["id"], kind="question")
    assert set(artifact) == {"question:M1"}
    for record in records:
        assert (
            artifact["question:" + record["record_key"]]["content_hash"] == record["content_hash"]
        )
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_question_file_submission_keeps_native_boundary_and_validation(
    setup_case, sandboxes, tmp_path
):
    service, owner, case = setup_case
    # The module shares VMs; production gives each worker a fresh one.
    assert sandboxes[0].execute("rm -rf /workspace/skills").exit_code == 0
    answer = question("T2")
    files = sandboxes[0].upload_files(
        [
            ("/workspace/answer.json", json.dumps(answer).encode()),
            ("/workspace/invalid-answer.json", b'{"question_id": "T2"}'),
        ]
    )
    assert all(item.error is None for item in files)
    attempts = [
        {"path": "/workspace/missing-answer.json"},
        {"path": "/etc/passwd"},
        {"path": "/workspace/invalid-answer.json"},
        {"payload": answer, "path": "/workspace/answer.json"},
        {"path": "/workspace/answer.json"},
    ]
    model = CorrectingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "submit_question", "args": args, "id": f"file-submit-{index}"}
                ],
            )
            for index, args in enumerate(attempts)
        ]
        + [AIMessage(content="The fictional T2 file is accepted.")]
    )
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        completed = await run_worker(
            service,
            owner=owner,
            assessment_id=case["id"],
            generation=case["generation"],
            role="technical",
            question_id="T2",
            sandbox=sandboxes[0],
            model=model,
            checkpointer=checkpointer,
            research=None,
            repository_cache=tmp_path,
            allowed_repositories=set(),
        )
    assert completed["status"] == "completed"
    assert len(model.observed_errors) >= 4
    # Pinned skills reach the model before its first decision, without a read_file call.
    assert "# Deployment analysis (T2)" in model.seen_text[0] or any(
        "# Deployment analysis (T2)" in text for text in model.seen_text[:4]
    )
    skills = sandboxes[0].execute("ls /workspace/skills").output.split()
    assert {"deployment-analysis", "repository-index", "code-exploration"} <= set(skills)
    assert "market-evidence" not in skills
    saved = service.state.records(owner, case["id"], kind="question")
    assert [(item["record_key"], item["version"]) for item in saved] == [("T2", 1)]
    assert len(service.state.records(owner, case["id"], kind="rejected_submission")) == 4
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_repeated_rejected_submissions_stop_the_worker_and_keep_drafts(
    setup_case, sandboxes, tmp_path
):
    service, owner, case = setup_case
    broken = question("T1") | {"details": {}}
    model = RehearsalModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "submit_question", "args": {"payload": broken}, "id": f"bad-{n}"}
                ],
            )
            for n in range(MAX_REJECTED_SUBMISSIONS + 2)
        ]
    )
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        with pytest.raises(CorrectionLimitReached):
            await run_worker(
                service,
                owner=owner,
                assessment_id=case["id"],
                generation=case["generation"],
                role="technical",
                question_id="T1",
                sandbox=sandboxes[0],
                model=model,
                checkpointer=checkpointer,
                research=None,
                repository_cache=tmp_path,
                allowed_repositories=set(),
            )
    rejected = service.state.records(owner, case["id"], kind="rejected_submission")
    assert len(rejected) == MAX_REJECTED_SUBMISSIONS
    assert rejected[0]["payload"]["draft"]["question_id"] == "T1"  # The rejected payload is kept.
    assert "Missing:" in rejected[0]["payload"]["errors"]
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    assert service.state.records(owner, case["id"], kind="question") == []


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
                question_id="B1",
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


@pytest.mark.asyncio
async def test_lead_delegates_to_a_subagent_and_submits_a_cited_analysis(
    setup_case, sandboxes, tmp_path, monkeypatch
):
    service, owner, case = setup_case
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/operators",
        original=b"Operators report upgrades keep breaking in production.",
        text="Operators report upgrades keep breaking in production.",
        media_type="text/plain",
        provenance={"provider": "fixture", "public": True},
        limitations=["Fictional fixture source."],
    )
    cited = {"source_id": source.id, "quote": "upgrades keep breaking"}
    claims = [
        {
            "id": "C1",
            "statement": "Operators report upgrade breakage.",
            "status": "observed",
            "citations": [cited],
        },
        {"id": "C2", "statement": "Teams would pay per database.", "status": "modelled"},
    ]
    option = {
        "open_part": "Engine",
        "paid_part": "Operations",
        "buyer": "Engineering lead",
        "why_pay": "Avoid incidents",
        "main_risk": "Self-hosting is cheap",
        "case_for": "Observed pain",
        "case_against": "No willingness-to-pay evidence",
    }
    analysis = {
        "thesis": "Keep the engine open; sell managed upgrades.",
        "summary": "Fictional analysis for an integration check.",
        "claims": claims,
        "asset": {
            "summary": "Fictional engine.",
            "ownership": "third_party_oss",
            "maturity": "usable",
            "operating_burden": ["C1"],
        },
        "rights": {
            "summary": "Fictional permissive license.",
            "can_open_source": "yes",
            "can_sell_hosted": "yes",
        },
        "buyer": {
            "summary": "Fictional buyer.",
            "user": "Engineer",
            "buyer": "Lead",
            "trigger": "Failed upgrade",
            "current_solution": "Manual",
            "claims": ["C1"],
        },
        "options": [
            option | {"id": "O1", "route": "managed_service", "claims": ["C1", "C2"]},
            option | {"id": "O2", "route": "services_only", "claims": ["C2"]},
        ],
    }
    code_model = CorrectingModel(
        model_name="condere:code",
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ClaimBatch",
                        "args": {
                            "claims": [
                                {
                                    "statement": "Upgrades break.",
                                    "status": "observed",
                                    "citations": [cited],
                                }
                            ]
                        },
                        "id": "structured",
                    }
                ],
            )
        ],
    )
    lead = CorrectingModel(
        model_name="condere:lead",
        responses=[
            AIMessage(content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "r"}]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Find operating burden in the code.",
                            "subagent_type": "code-analyst",
                        },
                        "id": "delegate",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "submit_analysis", "args": {"payload": analysis}, "id": "s"}],
            ),
            AIMessage(content="The fictional analysis has been submitted."),
        ],
    )
    worker = service.assign_worker(owner, case["id"], case["generation"], "lead")
    assert worker["assignment"]["permitted_tools"][-1] == "submit_analysis"
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        run = run_worker(
            service=service,
            owner=owner,
            assessment_id=case["id"],
            generation=case["generation"],
            role="lead",
            sandbox=sandboxes[0],
            model=lead,
            research=None,
            repository_cache=tmp_path,
            allowed_repositories=set(),
            checkpointer=checkpointer,
            code_model=code_model,
            extract_model=CorrectingModel(model_name="condere:extract", responses=[]),
        )
        try:
            completed = await run
        except Conflict as error:
            raise AssertionError(f"Lead tool errors: {lead.observed_errors}") from error
    assert completed["status"] == "completed"
    assert "task" in lead.tool_names and "submit_analysis" in lead.tool_names
    assert "submit_analysis" not in code_model.tool_names
    assert any("Upgrades break." in text for text in lead.seen_text)
    saved = service.analysis(owner, case["id"])
    assert saved.claims[0].citations[0].text_sha256 == source.text_sha256
    skills = sandboxes[0].execute("ls /workspace/skills").output.split()
    assert {"asset-understanding", "search-routing", "buyer-evidence"} <= set(skills)
    assert "market-evidence" not in skills
