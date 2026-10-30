"""Native spin-off coordination: lead, subagents and decision, with no paid providers."""

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr
from smol import ConnectOptions, Machine
from test_evidence import SimulatedObjects
from test_sandbox import RehearsalModel

from condere.runtime.coordinator import AssessmentCoordinator
from condere.runtime.evidence import EvidenceStore, object_client
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import AssessmentStore, Conflict, NotFound, RecoveryRequired

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1"
        or os.environ.get("CONDERE_TEST_SANDBOX") != "1",
        reason="Explicit PostgreSQL and native sandbox opt-in required",
    ),
]


class BlockingModel(RehearsalModel):
    _entered: asyncio.Event = PrivateAttr()
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._entered.set()
        await self._release.wait()
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ScriptedJev:
    """Answers like Jev, with probabilities only; never judges research quality."""

    def __init__(self):
        self.asked = []

    def ask(self, state, questions, purpose):
        self.asked.append(purpose)
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.9}
                continue
            keys = list(question["criteria"])
            chosen = {"verdict": "pursue", "option": "O1"}.get(name, keys[0])
            answers[name] = {
                "type": "choice",
                "choice": chosen,
                "probabilities": {
                    key: 0.8 if key == chosen else 0.2 / (len(keys) - 1) for key in keys
                },
                "confidence": 0.7,
            }
        return {"model": "typesafe/jev-1.14-20261014", "answers": answers}


BUILD_PLAN = {
    "option": "O1",
    "keep_open": [],
    "build_paid": [{"capability": "Managed upgrades", "why": ["C1"]}],
    "architecture_delta": ["tenancy", "metering"],
    "work_packages": [{"id": "W1", "goal": "Upgrade runner", "acceptance": "Upgrade test passes"}],
    "not_authorized": ["deployment", "outreach"],
}


def publish_spinoff(service, owner, case, source_id):
    """Publish a fictional result through the service contract, without VMs or providers."""
    from test_service import worker

    from condere.runtime.decision import decide

    generation = case["generation"]
    lead = worker(service, owner, case, "lead")
    quote = service.evidence.text(owner, case["id"], source_id)[:20]
    service.submit_analysis(
        owner, case["id"], generation, lead["id"], analysis_payload(source_id, quote)
    )
    service.finish_worker(owner, case["id"], generation, lead["id"])
    prompt = service.state.get(owner, case["id"])["brief"]["original_input"]
    decision, brief = decide(ScriptedJev(), service.analysis(owner, case["id"]), prompt, case["id"])
    service.record_decision(
        owner, case["id"], decision, brief, service.analysis_hash(owner, case["id"])
    )
    planner = worker(service, owner, case, "planner")
    service.submit_build_plan(owner, case["id"], generation, planner["id"], BUILD_PLAN)
    service.finish_worker(owner, case["id"], generation, planner["id"])
    return service.finalize_spinoff(owner, case["id"], generation)


def analysis_payload(source_id: str, quote: str = "upgrades keep breaking") -> dict:
    option = {
        "open_part": "Engine",
        "paid_part": "Operations",
        "buyer": "Engineering lead",
        "why_pay": "Avoid incidents",
        "main_risk": "Self-hosting is cheap",
        "case_for": "Observed pain",
        "case_against": "No willingness-to-pay evidence",
    }
    return {
        "thesis": "Keep the engine open; sell managed upgrades.",
        "summary": "Fictional analysis for coordination checks only.",
        "claims": [
            {
                "id": "C1",
                "statement": "Operators report upgrade breakage.",
                "status": "observed",
                "citations": [{"source_id": source_id, "quote": quote}],
            },
            {"id": "C2", "statement": "Teams would pay per database.", "status": "modelled"},
        ],
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


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    for key in (
        "OPENROUTER_API_KEY",
        "CONDERE_OPENROUTER_API_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "OPENAI_API_KEY",
        "EXA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    # LangSmith uses requests rather than httpx, so traces remain possible here.
    def no_http(transport, request):
        pytest.fail("Coordinator fixtures must not use real provider HTTP.")

    async def no_async_http(transport, request):
        pytest.fail("Coordinator fixtures must not use real provider HTTP.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_http)
    state = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    state.setup()
    native_objects = os.environ.get("CONDERE_TEST_OBJECTS") == "1"
    if native_objects and os.environ.get("CONDERE_EVIDENCE_ENDPOINT") != "http://127.0.0.1:19000":
        pytest.fail("Native coordinator object checks require the task's local evidence service.")
    objects = object_client(os.environ) if native_objects else SimulatedObjects()
    bucket = os.environ["CONDERE_EVIDENCE_BUCKET"] if native_objects else "condere-coordinator-test"
    service = AssessmentService(
        state,
        EvidenceStore(state, objects, bucket),
        execution_mode="simulation",
    )
    owner, campaign = "coordinator-test-" + str(uuid4()), str(uuid4())
    state.authorize_campaign(
        owner, campaign, 10_000_000, {"basis": "fictional native test; no spending"}
    )
    case = service.admit(
        owner,
        campaign,
        "intake",
        {
            "original_input": (
                "Assess a fictional library. No market or customer claims are established."
            ),
        },
    )
    created, calls = [], []
    create = SmolSandbox.create

    def tracked_create(*args, **kwargs):
        sandbox = create(*args, **kwargs)
        created.append(sandbox)
        return sandbox

    monkeypatch.setattr(SmolSandbox, "create", tracked_create)

    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/native-fixture",
        original=b"Operators report upgrades keep breaking in production.",
        text="Operators report upgrades keep breaking in production.",
        media_type="text/plain",
        provenance={"provider": "simulation", "public": True},
        limitations=["Tests coordination behavior; establishes no business finding."],
    )
    jev = ScriptedJev()

    def factory(context):
        calls.append((context.worker_role, context.generation))
        if context.worker_role == "planner":
            plan = BUILD_PLAN
            return RehearsalModel(
                model_name="condere:planner",
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "submit_build_plan", "args": {"payload": plan}, "id": "plan"}
                        ],
                    ),
                    AIMessage(content="The fictional plan was submitted."),
                ],
            )
        if context.worker_role != "lead":
            return RehearsalModel(model_name=f"condere:{context.worker_role}", responses=[])
        return RehearsalModel(
            model_name="condere:lead",
            responses=[
                AIMessage(
                    content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}]
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_analysis",
                            "args": {"payload": {}},
                            "id": "deliberately-invalid-analysis",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_analysis",
                            "args": {"payload": analysis_payload(source.id)},
                            "id": "submit",
                        }
                    ],
                ),
                AIMessage(content="The fictional analysis was submitted."),
            ],
        )

    def coordinator(model_factory=None, callbacks=None, tracing=None):
        return AssessmentCoordinator(
            service,
            checkpoint_dsn=os.environ["CONDERE_CHECKPOINT_URL"],
            application_dsn=os.environ["CONDERE_DATABASE_URL"],
            image=os.environ["CONDERE_SMOL_IMAGE"],
            artifacts=tmp_path,
            model_factory=model_factory or factory,
            decision_factory=lambda context: jev,
            callbacks=callbacks,
            tracing=tracing,
        )

    yield service, owner, case, coordinator, factory, calls, created, tmp_path, jev
    existing = {item.id for item in Machine.list(ConnectOptions(target="local"))}
    for sandbox in created:
        if sandbox.id in existing:
            sandbox.close(delete=True)
    if native_objects:
        objects.close()


@pytest.mark.asyncio
async def test_native_lead_run_records_analysis_and_one_decision(native_case):
    service, owner, case, make, _, calls, created, artifacts, jev = native_case
    result = await make().run(owner, case["id"])
    assert result.schema_version == "condere.spinoff.v1"
    assert result.decision.verdict.value == "pursue"
    assert result.decision.option.value == "O1"
    assert result.reasoning.thesis.startswith("Keep the engine open")
    assert service.decision(owner, case["id"])["analysis_hash"] == service.analysis_hash(
        owner, case["id"]
    )
    assert len(result.model_dump_json()) < 20_000
    assert service.state.get(owner, case["id"])["status"] == "completed"
    claims = service.artifact(owner, case["id"], "artifacts/claims.json")
    assert claims["sha256"] == result.claims.full.sha256
    assert "upgrades keep breaking" in claims["text"]
    reasoning = service.artifact(owner, case["id"], "artifacts/reasoning.md", limit=200)
    assert reasoning["next_offset"] == 200
    funnel = json.loads(service.artifact(owner, case["id"], "artifacts/funnel.json")["text"])
    assert funnel == service.funnel(owner, case["id"])
    assert funnel["stages"]["decisive"]["sources"] >= 1
    assert (await make().run(owner, case["id"])) == result
    assert service.report(owner, case["id"]) == result
    assert sorted(calls) == [("code-analyst", 1), ("extractor", 1), ("lead", 1), ("planner", 1)]
    assert result.build_plan.option == "O1" and result.build_plan.work_packages[0].id == "W1"
    assert jev.asked == ["assess option O1", "assess option O2", "choose verdict, option and route"]
    workers = service.state.workers(owner, case["id"])
    assert [worker["assignment"]["role"] for worker in workers] == ["lead", "planner"]
    assert {worker["status"] for worker in workers} == {"completed"}
    rejected = service.state.records(owner, case["id"], kind="rejected_submission")
    assert len(rejected) == 1 and rejected[0]["payload"]["tool"] == "submit_analysis"
    exports = service.state.records(owner, case["id"], kind="worker_export")
    assert len(exports) == 2
    for export in exports:
        raw = (artifacts / case["id"] / export["record_key"] / "accepted.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == export["payload"]["sha256"]
    [brief] = service.state.records(owner, case["id"], kind="decision_brief")
    assert brief["payload"]["sha256"] == result.decision.brief.sha256
    assert service.state.operations(owner, case["id"]) == []
    existing = {item.id for item in Machine.list(ConnectOptions(target="local"))}
    assert not {sandbox.id for sandbox in created} & existing


@pytest.mark.asyncio
async def test_duplicate_run_owner_checks_and_explicit_cancellation(native_case):
    service, owner, case, make, factory, calls, created, _, _ = native_case
    entered = asyncio.Event()

    def blocking(context):
        if context.worker_role == "lead":
            model = BlockingModel(
                model_name="condere:lead",
                responses=[AIMessage(content="Never finishes before cancellation.")],
            )
            model._entered = entered
            return model
        return factory(context)

    coordinator = make(blocking)
    running = asyncio.create_task(coordinator.run(owner, case["id"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=60)
        count = len(created)
        with pytest.raises(Conflict, match="live coordinator"):
            await make().run(owner, case["id"])
        with pytest.raises(NotFound):
            await coordinator.cancel("another-owner", case["id"])
        assert len(created) == count
        cancelled = await coordinator.cancel(owner, case["id"])
        assert cancelled["status"] == "cancelled"
        assert running.cancelled()
        remaining = {item.id: item.state for item in Machine.list(ConnectOptions(target="local"))}
        assert all(
            remaining.get(sandbox.id, "stopped") in {"stopped", "exited"} for sandbox in created
        )
    finally:
        if not running.done():
            await coordinator.cancel(owner, case["id"])


@pytest.mark.asyncio
async def test_continuation_after_an_unfinished_lead_reaches_the_decision(native_case):
    service, owner, case, make, factory, calls, _, _, jev = native_case

    def incomplete(context):
        if context.worker_role == "lead":
            return RehearsalModel(
                model_name="condere:lead",
                responses=[AIMessage(content="Simulated interruption without an analysis.")],
            )
        return factory(context)

    with pytest.raises(Conflict, match="without submitting"):
        await make(incomplete).run(owner, case["id"])
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    assert service.decision(owner, case["id"]) is None and jev.asked == []
    with pytest.raises(Conflict, match="explicit continuation"):
        await make().run(owner, case["id"])
    with pytest.raises(Conflict, match="private-query terms"):
        await make().run(owner, case["id"], resume=True, private_terms=("changed",))
    result = await make().run(owner, case["id"], resume=True)
    assert result.decision.verdict.value == "pursue"
    assert ("lead", 2) in calls
    assert service.state.spending(owner, case["id"])["budget"]["available_microusd"] == 10_000_000


@pytest.mark.asyncio
async def test_orphaned_dispatch_requires_reconciliation_before_continuation(native_case):
    service, owner, case, make, _, calls, created, _, _ = native_case
    started = service.begin(owner, case["id"])
    operation = service.state.reserve_operation(
        owner,
        case["id"],
        started["generation"],
        "unknown-request",
        "simulation",
        {"test": True},
        7000,
    )
    service.state.dispatch(
        owner, case["id"], started["generation"], operation["id"], {"provider": "simulation"}
    )
    with pytest.raises(RecoveryRequired, match="Reconcile"):
        await make().run(owner, case["id"], resume=True)
    assert calls == created == []
    saved = service.state.operations(owner, case["id"])[0]
    assert saved["state"] == "uncertain"
    assert saved["reserved_microusd"] == 7000
    assert saved["actual_microusd"] is None


def test_repository_allowance_is_shared_and_atomic(native_case):
    service, owner, case, _, _, _, _, _, _ = native_case
    started = service.begin(owner, case["id"])

    def admit(index):
        try:
            return service.state.admit_repository(
                owner,
                case["id"],
                started["generation"],
                f"https://github.com/example/project{index}",
                {"basis": "fixture admission"},
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = list(pool.map(admit, range(4)))
    assert len([item for item in admitted if item is not None]) == 3
    assert len(service.state.records(owner, case["id"], kind="repository_admission")) == 3
