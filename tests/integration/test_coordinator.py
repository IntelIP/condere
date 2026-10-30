"""Complete native assessment coordination with fictional findings and no paid providers."""

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
from test_service import question, review

from condere.runtime.contracts import MARKET_IDS, QUESTION_IDS, TECHNICAL_IDS
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


@pytest.fixture
def native_case(tmp_path, monkeypatch, request):
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
            "analysis_depth": getattr(request, "param", "deep"),
        },
    )
    created, calls = [], []
    create = SmolSandbox.create

    def tracked_create(*args, **kwargs):
        sandbox = create(*args, **kwargs)
        created.append(sandbox)
        return sandbox

    monkeypatch.setattr(SmolSandbox, "create", tracked_create)

    def accepted():
        return {item["record_key"] for item in state.records(owner, case["id"], kind="question")}

    def factory(context):
        identity = context.question_id
        calls.append((identity or context.worker_role, context.generation))
        if identity is not None:
            # Dependency order: T1-T8, then T9, then Market, then Business.
            required = {
                "T9": set(TECHNICAL_IDS) - {"T9"},
                "M": set(TECHNICAL_IDS),
                "B": set(TECHNICAL_IDS) | set(MARKET_IDS),
            }.get(identity if identity == "T9" else identity[0], set())
            assert required <= accepted()
            submissions = [
                {"name": "submit_question", "args": {"payload": question(identity)}, "id": identity}
            ]
        else:
            assert accepted() == set(QUESTION_IDS)
            submissions = [
                {
                    "name": "submit_review",
                    "args": {"payload": review(service, owner, case)},
                    "id": "review",
                }
            ]
        return RehearsalModel(
            responses=[
                AIMessage(
                    content="", tool_calls=[{"name": "read_assessment", "args": {}, "id": "read"}]
                ),
                *(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "submit_question",
                                    "args": {"payload": {}},
                                    "id": "deliberately-invalid-technical",
                                }
                            ],
                        )
                    ]
                    if identity == "T1"
                    else []
                ),
                AIMessage(content="", tool_calls=submissions),
                AIMessage(content="All assigned fictional records were submitted."),
            ]
        )

    def coordinator(model_factory=None, callbacks=None, tracing=None):
        return AssessmentCoordinator(
            service,
            checkpoint_dsn=os.environ["CONDERE_CHECKPOINT_URL"],
            application_dsn=os.environ["CONDERE_DATABASE_URL"],
            image=os.environ["CONDERE_SMOL_IMAGE"],
            artifacts=tmp_path,
            model_factory=model_factory or factory,
            callbacks=callbacks,
            tracing=tracing,
        )

    if native_objects:
        service.evidence.save_source(
            owner,
            case["id"],
            kind="web",
            origin="https://example.com/native-storage-fixture",
            original=b"Fictional source for the native storage integration check.",
            text="Fictional source for the native storage integration check.",
            media_type="text/plain",
            provenance={"provider": "simulation", "storage": "native"},
            limitations=["Tests storage behavior; establishes no business finding."],
        )
    yield service, owner, case, coordinator, factory, calls, created, tmp_path
    existing = {item.id for item in Machine.list(ConnectOptions(target="local"))}
    for sandbox in created:
        if sandbox.id in existing:
            sandbox.close(delete=True)
    if native_objects:
        objects.close()


@pytest.mark.asyncio
async def test_complete_native_team_publishes_one_saved_dossier(native_case):
    service, owner, case, make, _, calls, created, artifacts = native_case
    coordinator = make()
    dossier = await coordinator.run(owner, case["id"])
    assert dossier.execution_mode == "simulation"
    assert len(dossier.findings.questions) == 26
    assert len(dossier.review.items) == 26
    order = [name for name, _ in calls]
    assert set(order[:8]) == set(TECHNICAL_IDS) - {"T9"} and order[8] == "T9"
    assert set(order[9:17]) == set(MARKET_IDS) and order[-1] == "review"
    workers = service.state.workers(owner, case["id"])
    assert len(workers) == 27
    assert {worker["status"] for worker in workers} == {"completed"}
    assert len({worker["machine_id"] for worker in workers}) == 27
    assert len({worker["thread_id"] for worker in workers}) == 27
    rejected = service.state.records(owner, case["id"], kind="rejected_submission")
    assert len(rejected) == 1 and rejected[0]["payload"]["tool"] == "submit_question"
    exports = service.state.records(owner, case["id"], kind="worker_export")
    for record in exports:
        raw = (artifacts / case["id"] / record["record_key"] / "accepted.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["payload"]["sha256"]
        assert {key: value["content_hash"] for key, value in json.loads(raw).items()} == (
            record["payload"]["accepted_hashes"]
        )
    assert len(exports) == 27
    assert dossier.model_dump(mode="json") == (await make().run(owner, case["id"])).model_dump(
        mode="json"
    )
    assert len(calls) == 27
    assert service.state.operations(owner, case["id"]) == []
    existing = {item.id for item in Machine.list(ConnectOptions(target="local"))}
    assert not {sandbox.id for sandbox in created} & existing


@pytest.mark.asyncio
async def test_duplicate_run_owner_checks_and_explicit_cancellation(native_case):
    service, owner, case, make, factory, calls, created, _ = native_case
    entered = asyncio.Event()

    def blocking(context):
        if context.worker_role == "market":
            model = BlockingModel(
                responses=[AIMessage(content="Never finishes before cancellation.")]
            )
            model._entered = entered
            return model
        return factory(context)

    coordinator = make(blocking)
    running = asyncio.create_task(coordinator.run(owner, case["id"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        count = len(created)
        with pytest.raises(Conflict, match="live coordinator"):
            await make().run(owner, case["id"])
        with pytest.raises(NotFound):
            await coordinator.cancel("another-owner", case["id"])
        assert len(created) == count
        cancelled = await coordinator.cancel(owner, case["id"])
        assert cancelled["status"] == "cancelled"
        assert running.cancelled()
        assert {item["status"] for item in service.state.workers(owner, case["id"])} <= {
            "completed",
            "interrupted",
        }
        remaining = {item.id: item.state for item in Machine.list(ConnectOptions(target="local"))}
        assert all(
            remaining.get(sandbox.id, "stopped") in {"stopped", "exited"} for sandbox in created
        )
    finally:
        if not running.done():
            await coordinator.cancel(owner, case["id"])


@pytest.mark.asyncio
async def test_continuation_preserves_answers_and_reuses_finished_research_roles(native_case):
    service, owner, case, make, factory, calls, _, _ = native_case

    def incomplete(context):
        if context.question_id == "B2":
            return RehearsalModel(
                responses=[AIMessage(content="Simulated interruption without a B2 record.")]
            )
        return factory(context)

    with pytest.raises(ExceptionGroup):
        await make(incomplete).run(owner, case["id"])
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    before = {
        item["record_key"]: item["content_hash"]
        for item in service.state.records(owner, case["id"], kind="question")
    }
    assert set(TECHNICAL_IDS) | set(MARKET_IDS) <= set(before) and "B2" not in before
    count = len(calls)
    with pytest.raises(Conflict, match="explicit continuation"):
        await make().run(owner, case["id"])
    with pytest.raises(Conflict, match="private-query terms"):
        await make().run(owner, case["id"], resume=True, private_terms=("changed",))
    dossier = await make().run(owner, case["id"], resume=True)
    assert len(dossier.findings.questions) == 26
    resumed = calls[count:]
    assert ("B2", 2) in resumed and resumed[-1] == ("review", 2)
    assert all(name.startswith("B") or name == "review" for name, _ in resumed)
    after = service.state.records(owner, case["id"], kind="question")
    assert len(after) == 26
    assert all(
        item["content_hash"] == before[item["record_key"]]
        for item in after
        if item["record_key"] in before
    )
    assert service.state.spending(owner, case["id"])["budget"]["available_microusd"] == 10_000_000


@pytest.mark.asyncio
async def test_orphaned_dispatch_requires_reconciliation_before_continuation(native_case):
    service, owner, case, make, _, calls, created, _ = native_case
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
    service, owner, case, _, _, _, _, _ = native_case
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
