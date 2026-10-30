"""Real SmolVM, Deep Agents, PostgreSQL and checkpoints with a labelled deterministic model."""

import json
import os

import pytest
from langchain_core.messages import AIMessage
from pydantic import Field
from test_coordinator import BUILD_PLAN
from test_sandbox import RehearsalModel
from test_sandbox import sandboxes as sandboxes
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


def decided(service, owner, case):
    """Save an analysis and a scripted decision, so the planner has a chosen option."""
    from test_coordinator import ScriptedJev, analysis_payload
    from test_service import saved_source, worker

    from condere.runtime.decision import decide

    source = saved_source(service, owner, case)
    lead = worker(service, owner, case, "lead")
    service.submit_analysis(
        owner, case["id"], case["generation"], lead["id"], analysis_payload(source.id)
    )
    service.finish_worker(owner, case["id"], case["generation"], lead["id"])
    prompt = service.state.get(owner, case["id"])["brief"]["original_input"]
    decision, brief = decide(ScriptedJev(), service.analysis(owner, case["id"]), prompt, case["id"])
    service.record_decision(
        owner, case["id"], decision, brief, service.analysis_hash(owner, case["id"])
    )


def planner_args(service, owner, case, sandbox, model, tmp_path):
    return dict(
        service=service,
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        role="planner",
        sandbox=sandbox,
        model=model,
        research=None,
        repository_cache=tmp_path,
        allowed_repositories=set(),
    )


@pytest.mark.asyncio
async def test_assigned_worker_saves_its_result_and_reuses_completed_checkpoint(
    setup_case, sandboxes, tmp_path, monkeypatch
):
    service, owner, case = setup_case
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    decided(service, owner, case)
    model = CorrectingModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}]
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_build_plan",
                        "args": {"payload": BUILD_PLAN | {"option": "O2"}},
                        "id": "wrong-option",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "submit_build_plan", "args": {"payload": BUILD_PLAN}, "id": "plan"}
                ],
            ),
            AIMessage(content="The fictional build plan has been submitted."),
        ]
    )
    args = planner_args(service, owner, case, sandboxes[0], model, tmp_path)
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
        assert any("chosen option O1" in message for message in model.observed_errors)
    calls = len(model.seen_system)
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as reopened:
        assert await run_worker(**args, checkpointer=reopened) == completed
    assert len(model.seen_system) == calls
    assert {"submit_build_plan", "inspect_repository"} <= set(model.tool_names)
    assert not {"submit_analysis", "submit_claims", "search_public", "task"} & set(model.tool_names)
    skills = sandboxes[0].execute("ls /workspace/skills").output.split()
    assert sorted(skills) == ["build-plan", "code-exploration", "repository-index"]
    exported = sandboxes[0].download_files(["/workspace/results/planner.json"])[0]
    assert exported.error is None
    artifact = json.loads(exported.content)
    [record] = service.state.records(owner, case["id"], kind="build_plan")
    assert artifact == {
        "build_plan:main": {"content_hash": record["content_hash"], "payload": record["payload"]}
    }
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_file_submission_keeps_native_boundary_and_validation(
    setup_case, sandboxes, tmp_path
):
    service, owner, case = setup_case
    decided(service, owner, case)
    # The module shares VMs; production gives each worker a fresh one.
    assert sandboxes[0].execute("rm -rf /workspace/skills").exit_code == 0
    files = sandboxes[0].upload_files(
        [
            ("/workspace/plan.json", json.dumps(BUILD_PLAN).encode()),
            ("/workspace/invalid-plan.json", b'{"option": "O1"}'),
        ]
    )
    assert all(item.error is None for item in files)
    attempts = [
        {"path": "/workspace/missing-plan.json"},
        {"path": "/etc/passwd"},
        {"path": "/workspace/invalid-plan.json"},
        {"payload": BUILD_PLAN, "path": "/workspace/plan.json"},
        {"path": "/workspace/plan.json"},
    ]
    model = CorrectingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "submit_build_plan", "args": args, "id": f"file-submit-{index}"}
                ],
            )
            for index, args in enumerate(attempts)
        ]
        + [AIMessage(content="The fictional plan file is accepted.")]
    )
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        completed = await run_worker(
            **planner_args(service, owner, case, sandboxes[0], model, tmp_path),
            checkpointer=checkpointer,
        )
    assert completed["status"] == "completed"
    assert len(model.observed_errors) >= 4
    # Pinned skills reach the model before its first decision, without a read_file call.
    assert any("# Build plan" in text for text in model.seen_text[:4])
    saved = service.state.records(owner, case["id"], kind="build_plan")
    assert [(item["record_key"], item["version"]) for item in saved] == [("main", 1)]
    assert len(service.state.records(owner, case["id"], kind="rejected_submission")) == 4
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_repeated_rejected_submissions_stop_the_worker_and_keep_drafts(
    setup_case, sandboxes, tmp_path
):
    service, owner, case = setup_case
    decided(service, owner, case)
    broken = {key: value for key, value in BUILD_PLAN.items() if key != "not_authorized"}
    model = RehearsalModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "submit_build_plan", "args": {"payload": broken}, "id": f"bad-{n}"}
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
                **planner_args(service, owner, case, sandboxes[0], model, tmp_path),
                checkpointer=checkpointer,
            )
    rejected = service.state.records(owner, case["id"], kind="rejected_submission")
    assert len(rejected) == MAX_REJECTED_SUBMISSIONS
    assert rejected[0]["payload"]["draft"]["option"] == "O1"  # The rejected payload is kept.
    assert "not_authorized" in rejected[0]["payload"]["errors"]
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    assert service.state.records(owner, case["id"], kind="build_plan") == []


@pytest.mark.asyncio
async def test_worker_cannot_finish_without_its_accepted_result(setup_case, sandboxes, tmp_path):
    service, owner, case = setup_case
    decided(service, owner, case)
    model = RehearsalModel(responses=[AIMessage(content="Done, with no accepted records.")])
    async with checkpoint_store(
        os.environ["CONDERE_CHECKPOINT_URL"], application_dsn=os.environ["CONDERE_DATABASE_URL"]
    ) as checkpointer:
        with pytest.raises(Conflict, match="without submitting"):
            await run_worker(
                **planner_args(service, owner, case, sandboxes[1], model, tmp_path),
                checkpointer=checkpointer,
            )
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    assert service.state.records(owner, case["id"], kind="build_plan") == []


@pytest.mark.asyncio
async def test_lead_delegates_to_a_subagent_and_submits_a_cited_analysis(
    setup_case, sandboxes, tmp_path, monkeypatch
):
    service, owner, case = setup_case
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    # The module shares VMs; production gives each worker a fresh one.
    assert sandboxes[0].execute("rm -rf /workspace/skills").exit_code == 0
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
        "next_test": {
            "assumption": "Teams pay to delegate upgrades.",
            "method": "Priced interviews",
            "success": "Three priced commitments",
            "failure": "No priced interest",
            "cost_and_time": "Two weeks",
        },
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
    assert "build-plan" not in skills
