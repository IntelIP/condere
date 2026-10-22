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

from condere.runtime.contracts import TECHNICAL_FIELDS
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

    local_tracing = os.environ.get("CONDERE_TEST_LANGFUSE") == "1"
    if local_tracing and os.environ.get("LANGFUSE_BASE_URL") != "http://127.0.0.1:3000":
        pytest.fail("Native tracing checks require the task's local Langfuse service.")
    sync_request = httpx.HTTPTransport.handle_request
    async_request = httpx.AsyncHTTPTransport.handle_async_request

    def is_local_trace(request):
        return local_tracing and (request.url.scheme, request.url.host, request.url.port) == (
            "http",
            "127.0.0.1",
            3000,
        )

    def no_http(transport, request):
        if is_local_trace(request):
            return sync_request(transport, request)
        pytest.fail("Coordinator fixtures must not use real provider HTTP.")

    async def no_async_http(transport, request):
        if is_local_trace(request):
            return await async_request(transport, request)
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

    def factory(context):
        calls.append((context.worker_role, context.generation))
        role = context.worker_role
        if role == "technical":
            assignment = next(
                item["assignment"]
                for item in state.workers(owner, case["id"])
                if item["worker_key"] == role and item["generation"] == context.generation
            )
            unknown = {
                "conclusion": "Unavailable in this fictional fixture.",
                "availability": "unavailable",
                "limitations": ["No real research occurred."],
            }
            payload = {
                "findings": {field: unknown for field in TECHNICAL_FIELDS},
                "capabilities": [
                    item | {"status": "unavailable", "value": None, "finding": unknown}
                    for item in assignment["required_capabilities"]
                ],
                "inspection_limitations": [
                    "Deterministic fixture; no business conclusion is proven."
                ],
            }
            submissions = [
                {"name": "submit_technical", "args": {"payload": payload}, "id": "technical"}
            ]
        elif role in {"market", "business"}:
            if role == "business":
                assert len(state.records(owner, case["id"], kind="technical")) == 1
                assert {
                    item["record_key"] for item in state.records(owner, case["id"], kind="question")
                } >= {"M" + str(number) for number in range(1, 9)}
            identities = (
                ["M" + str(number) for number in range(1, 9)]
                if role == "market"
                else ["B" + str(number) for number in range(1, 10)]
            )
            submissions = [
                {"name": "submit_question", "args": {"payload": question(identity)}, "id": identity}
                for identity in identities
            ]
        else:
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
                                    "name": "submit_technical",
                                    "args": {"payload": {}},
                                    "id": "deliberately-invalid-technical",
                                }
                            ],
                        )
                    ]
                    if role == "technical"
                    else []
                ),
                AIMessage(content="", tool_calls=submissions),
                AIMessage(content="All assigned fictional records were submitted."),
            ]
        )

    def coordinator(model_factory=None, callbacks=None, telemetry=None):
        return AssessmentCoordinator(
            service,
            checkpoint_dsn=os.environ["CONDERE_CHECKPOINT_URL"],
            application_dsn=os.environ["CONDERE_DATABASE_URL"],
            image=os.environ["CONDERE_SMOL_IMAGE"],
            artifacts=tmp_path,
            model_factory=model_factory or factory,
            callbacks=callbacks,
            telemetry=telemetry,
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
    assert len(dossier.findings.questions) == 17
    assert len(dossier.findings.technical.findings) == 13
    assert len(dossier.review.items) == 18
    assert set(calls[:2]) == {("technical", 1), ("market", 1)}
    assert calls[2:] == [("business", 1), ("review", 1)]
    workers = service.state.workers(owner, case["id"])
    assert len(workers) == 4
    assert {worker["status"] for worker in workers} == {"completed"}
    assert len({worker["machine_id"] for worker in workers}) == 4
    assert len({worker["thread_id"] for worker in workers}) == 4
    exports = service.state.records(owner, case["id"], kind="worker_export")
    for record in exports:
        raw = (artifacts / case["id"] / record["record_key"] / "accepted.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["payload"]["sha256"]
        assert {key: value["content_hash"] for key, value in json.loads(raw).items()} == (
            record["payload"]["accepted_hashes"]
        )
    assert len(exports) == 4
    assert dossier.model_dump(mode="json") == (await make().run(owner, case["id"])).model_dump(
        mode="json"
    )
    assert len(calls) == 4
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
        if context.worker_role == "business":
            return RehearsalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "submit_question",
                                "args": {"payload": question("B1")},
                                "id": "B1",
                            }
                        ],
                    ),
                    AIMessage(content="Simulated interruption after one accepted Business record."),
                ]
            )
        return factory(context)

    with pytest.raises(ExceptionGroup):
        await make(incomplete).run(owner, case["id"])
    assert service.state.get(owner, case["id"])["status"] == "interrupted"
    before = {
        item["record_key"]: item["content_hash"]
        for item in service.state.records(owner, case["id"], kind="question")
    }
    assert len(before) == 9
    count = len(calls)
    with pytest.raises(Conflict, match="explicit continuation"):
        await make().run(owner, case["id"])
    with pytest.raises(Conflict, match="capability requirements"):
        await make().run(owner, case["id"], resume=True, required_capabilities=[])
    dossier = await make().run(owner, case["id"], resume=True)
    assert len(dossier.findings.questions) == 17
    assert calls[count:] == [("business", 2), ("review", 2)]
    after = service.state.records(owner, case["id"], kind="question")
    assert len(after) == 17
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
