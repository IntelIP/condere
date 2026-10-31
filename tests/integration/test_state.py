"""Real PostgreSQL transactions with simulated providers; no inference or public research."""

import asyncio
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict
from uuid import uuid4

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.conninfo import conninfo_to_dict

from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.operations import (
    ProviderRefused,
    ProviderResult,
    ProviderUncertain,
    execute_once,
)
from condere.runtime.state import (
    AssessmentStore,
    BudgetExceeded,
    Conflict,
    NotFound,
    RecoveryRequired,
    content_hash,
    money,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


@pytest.fixture
def store():
    result = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    result.setup()
    return result


def campaign(store, *, cap=1_000_000, cases=1):
    owner, campaign_id = "test-owner-" + str(uuid4()), "test-campaign-" + str(uuid4())
    store.authorize_campaign(
        owner,
        campaign_id,
        cap,
        {"basis": "simulated provider test; no spending"},
        assessment_limit=cases,
    )
    return owner, campaign_id


def admit(store, owner, campaign_id, key="intake"):
    return store.admit(
        owner,
        campaign_id,
        key,
        {"original": "A technology, with no assumed customers."},
        policy_version="test-policy",
        method_version="test-method",
    )


def running(store, *, cap=1_000_000):
    owner, campaign_id = campaign(store, cap=cap)
    assessment = admit(store, owner, campaign_id)
    return owner, store.begin(owner, assessment["id"])


def reserve(store, owner, case, key="search", cost=400_000):
    return store.reserve_operation(
        owner,
        case["id"],
        case["generation"],
        key,
        "simulated-search",
        {"query": "public technology alternatives"},
        cost,
    )


def test_concurrent_admission_reuses_original_and_enforces_owner_and_authority(store):
    owner, campaign_id = campaign(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda _: admit(store, owner, campaign_id), range(8)))
    assert len({case["id"] for case in admitted}) == 1
    case = admitted[0]
    with pytest.raises(Conflict, match="different content"):
        store.admit(
            owner,
            campaign_id,
            "intake",
            {"original": "Changed idea"},
            policy_version="test-policy",
            method_version="test-method",
        )
    with pytest.raises(Conflict, match="already used"):
        admit(store, owner, campaign_id, "another-intake")
    with pytest.raises(Conflict, match="enlarged"):
        store.authorize_campaign(owner, campaign_id, 10_000_000, {"basis": "new unapproved budget"})
    for read in (store.get, store.records, store.operations, store.workers):
        with pytest.raises(NotFound):
            read("another-owner", case["id"])
    with pytest.raises(NotFound):
        store.begin("another-owner", case["id"])
    assert store.get(owner, case["id"])["brief"] == case["brief"]


def test_concurrent_reservations_share_campaign_cap_and_dispatch_once(store):
    owner, campaign_id = campaign(store, cases=2)
    cases = [
        store.begin(owner, admit(store, owner, campaign_id, str(index))["id"]) for index in range(2)
    ]

    def attempt(index):
        try:
            return reserve(store, owner, cases[index % 2], str(index), 100_000)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = [result for result in pool.map(attempt, range(24)) if result]
    assert len(results) == 10
    assert store.get(owner, cases[0]["id"])["budget"] == {
        "cap_microusd": 1_000_000,
        "pending_microusd": 1_000_000,
        "settled_microusd": 0,
        "available_microusd": 0,
    }
    operation = results[0]
    case = next(case for case in cases if case["id"] == operation["assessment_id"])
    side_effects = []

    def dispatch(_):
        try:
            store.dispatch(
                owner,
                case["id"],
                case["generation"],
                operation["id"],
                {"provider": "simulation", "request_id": operation["id"]},
            )
        except Conflict:
            return
        side_effects.append("one simulated external side effect")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(dispatch, range(8)))
    assert len(side_effects) == 1
    store.finish_operation(
        owner,
        case["id"],
        operation["id"],
        {"evidence": "saved simulation"},
        succeeded=True,
        actual_microusd=60_000,
        cost_basis={"basis": "fixture accounting"},
    )
    repeated = reserve(store, owner, case, operation["operation_key"], 100_000)
    assert repeated["result"] == {"evidence": "saved simulation"}
    assert repeated["state"] == "succeeded"
    with pytest.raises(Conflict, match="dispatch again"):
        store.dispatch(
            owner, case["id"], case["generation"], operation["id"], {"request_id": "repeat"}
        )
    assert store.get(owner, case["id"])["budget"]["available_microusd"] == 40_000


def test_interruption_preserves_evidence_and_requires_reconciliation_before_explicit_resume(store):
    owner, case = running(store)
    original = store.append_record(
        owner, case["id"], "analysis", "main", {"status": "limited", "sources": ["sha256:original"]}
    )
    pending = reserve(store, owner, case)
    unused = reserve(store, owner, case, "not-dispatched", 300_000)
    worker = store.assign_worker(
        owner,
        case["id"],
        case["generation"],
        "lead",
        {"role": "lead", "output": "/workspace/results/lead.json"},
    )
    store.attach_machine(
        owner, case["id"], case["generation"], worker["id"], "simulated-machine-id"
    )
    store.dispatch(
        owner, case["id"], case["generation"], pending["id"], {"request_id": "simulated-request"}
    )
    store.stop(owner, case["id"])

    # A fresh store/connection represents a restarted worker process.
    reopened = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    assert reopened.records(owner, case["id"])[0]["content_hash"] == original["content_hash"]
    assert reopened.workers(owner, case["id"])[0]["machine_id"] == "simulated-machine-id"
    states = {
        operation["id"]: operation["state"] for operation in reopened.operations(owner, case["id"])
    }
    assert states == {pending["id"]: "uncertain", unused["id"]: "released"}
    assert reopened.get(owner, case["id"])["budget"]["pending_microusd"] == 400_000
    with pytest.raises(RecoveryRequired):
        reopened.begin(owner, case["id"], resume=True)
    with pytest.raises(RecoveryRequired):
        reopened.finish_operation(owner, case["id"], pending["id"], {}, succeeded=True)
    reopened.finish_operation(
        owner,
        case["id"],
        pending["id"],
        {"result": "retrieved provider receipt"},
        succeeded=True,
        actual_microusd=250_000,
        cost_basis={"basis": "simulated invoice"},
        reconciliation={"source": "simulated provider request lookup"},
    )
    resumed = reopened.begin(owner, case["id"], resume=True)
    assert resumed["campaign_id"] == case["campaign_id"]
    assert resumed["generation"] == case["generation"] + 1
    with pytest.raises(Conflict, match="generation"):
        reserve(reopened, owner, case, "stale-worker", 1)
    assert reserve(reopened, owner, resumed, "not-dispatched", 300_000)["id"] == unused["id"]
    with pytest.raises(Conflict, match="latest accepted"):
        reopened.append_record(owner, case["id"], "analysis", "main", {"status": "qualified"})
    revised = reopened.append_record(
        owner,
        case["id"],
        "analysis",
        "main",
        {"status": "qualified"},
        supersedes_hash=original["content_hash"],
    )
    assert revised["version"] == 2
    assert [record["payload"] for record in reopened.records(owner, case["id"])] == [
        original["payload"],
        revised["payload"],
    ]


def test_cancellation_blocks_new_dispatch_and_preserves_pending_bills(store):
    owner, case = running(store)
    dispatched = reserve(store, owner, case)
    unused = reserve(store, owner, case, "unused")
    store.dispatch(
        owner, case["id"], case["generation"], dispatched["id"], {"request_id": "simulation"}
    )
    store.stop(owner, case["id"], cancel=True)
    with pytest.raises(Conflict):
        store.dispatch(
            owner, case["id"], case["generation"], unused["id"], {"request_id": "cancelled"}
        )
    with pytest.raises(Conflict):
        reserve(store, owner, case, "new-work", 1)
    store.finish_operation(
        owner,
        case["id"],
        dispatched["id"],
        {"error": "simulated provider failure"},
        succeeded=False,
        reconciliation={"source": "saved failed response"},
    )
    assert store.get(owner, case["id"])["budget"]["pending_microusd"] == 400_000
    assert store.get(owner, case["id"])["status"] == "cancelled"
    store.settle_cost(
        owner, case["id"], dispatched["id"], 170_000, {"basis": "simulated charge despite failure"}
    )
    assert store.get(owner, case["id"])["budget"]["pending_microusd"] == 0
    assert store.get(owner, case["id"])["budget"]["settled_microusd"] == 170_000
    with pytest.raises(Conflict, match="rewritten"):
        store.settle_cost(
            owner, case["id"], dispatched["id"], 0, {"basis": "incorrectly assumed free"}
        )


def test_actual_cost_overrun_is_saved_and_blocks_continuation(store):
    owner, case = running(store, cap=1_000)
    operation = reserve(store, owner, case, cost=800)
    unused = reserve(store, owner, case, "unused", 200)
    store.dispatch(
        owner, case["id"], case["generation"], operation["id"], {"request_id": "simulation"}
    )
    store.finish_operation(
        owner,
        case["id"],
        operation["id"],
        {"result": "saved even with a billing overrun"},
        succeeded=True,
        actual_microusd=1_200,
        cost_basis={"basis": "simulated upstream overrun"},
    )
    saved = store.get(owner, case["id"])
    assert saved["status"] == "interrupted"
    assert saved["budget"]["settled_microusd"] == 1_200
    assert saved["budget"]["available_microusd"] == -200
    assert (
        next(
            operation
            for operation in store.operations(owner, case["id"])
            if operation["id"] == unused["id"]
        )["state"]
        == "released"
    )
    with pytest.raises(BudgetExceeded):
        store.begin(owner, case["id"], resume=True)


def crash_after_dispatch(owner, case):
    store = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])
    store.append_record(
        owner, case["id"], "finding", "before-crash", {"evidence": "original retained"}
    )
    operation = reserve(store, owner, case, "crash")
    store.dispatch(
        owner, case["id"], case["generation"], operation["id"], {"request_id": "crashed-simulation"}
    )
    os._exit(17)


def test_real_worker_process_loss_preserves_results_and_blocks_ambiguous_replay(store):
    owner, case = running(store)
    process = multiprocessing.get_context("spawn").Process(
        target=crash_after_dispatch, args=(owner, case)
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("Simulated worker did not exit.")
    assert process.exitcode == 17
    assert store.records(owner, case["id"])[0]["payload"] == {"evidence": "original retained"}
    assert store.operations(owner, case["id"])[0]["state"] == "dispatched"
    assert store.get(owner, case["id"])["budget"]["pending_microusd"] == 400_000
    store.stop(owner, case["id"], generation=case["generation"])
    with pytest.raises(RecoveryRequired):
        store.begin(owner, case["id"], resume=True)


def test_provider_wrapper_reuses_saved_results_and_keeps_failure_liability(store):
    owner, case = running(store)
    calls = []

    def simulated_provider():
        calls.append("one simulated call")
        return ProviderResult(
            {"answer": "saved"}, actual_microusd=120_000, cost_basis={"basis": "simulated usage"}
        )

    options = dict(
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        key="model",
        kind="simulation",
        request={"prompt": "fixture"},
        max_microusd=400_000,
        dispatch_identity={"provider": "simulation"},
        invoke=simulated_provider,
    )
    first = execute_once(store, **options)
    saved = execute_once(AssessmentStore(os.environ["CONDERE_DATABASE_URL"]), **options)
    assert len(calls) == 1
    assert saved["id"] == first["id"]
    assert saved["result"] == {"answer": "saved"}

    def timeout():
        calls.append("unknown simulated charge")
        raise TimeoutError("simulated response loss")

    with pytest.raises(ProviderUncertain):
        execute_once(store, **(options | {"key": "lost-response", "invoke": timeout}))
    # Only the lost call is held: it keeps its reservation and is never bought again, while
    # the assessment continues and cannot be delivered until the call is reconciled.
    assert store.get(owner, case["id"])["status"] == "running"
    assert store.get(owner, case["id"])["budget"]["pending_microusd"] == 400_000
    with pytest.raises(ProviderUncertain):
        execute_once(store, **(options | {"key": "lost-response", "invoke": timeout}))
    assert len(calls) == 2
    with pytest.raises(RecoveryRequired):
        store.publish_dossier(owner, case["id"], case["generation"], {"result": "x"}, {})

    def closed_client():
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    with pytest.raises(ProviderRefused):
        execute_once(store, **(options | {"key": "never-sent", "invoke": closed_client}))
    never_sent = next(
        item
        for item in store.operations(owner, case["id"])
        if item["operation_key"] == "never-sent"
    )
    assert (never_sent["state"], never_sent["actual_microusd"]) == ("failed", 0)

    def worker_lost():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        execute_once(store, **(options | {"key": "worker-lost", "invoke": worker_lost}))
    assert store.get(owner, case["id"])["status"] == "interrupted"


def test_provider_response_after_cancellation_is_saved_without_restarting_work(store):
    owner, case = running(store)

    def cancelled_during_call():
        store.stop(owner, case["id"], cancel=True)
        return ProviderResult(
            {"partial": "useful result"},
            actual_microusd=100_000,
            cost_basis={"basis": "simulated usage"},
        )

    saved = execute_once(
        store,
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        key="model",
        kind="simulation",
        request={},
        max_microusd=400_000,
        dispatch_identity={"request_id": "simulation"},
        invoke=cancelled_during_call,
    )
    assert saved["state"] == "succeeded"
    assert saved["result"] == {"partial": "useful result"}
    assert store.get(owner, case["id"])["status"] == "cancelled"


class CheckpointState(TypedDict):
    accepted: int
    reviewed: bool


@pytest.mark.asyncio
async def test_separate_checkpoint_database_resumes_saved_graph(store):
    owner, case = running(store)
    worker = store.assign_worker(
        owner,
        case["id"],
        case["generation"],
        "planner",
        {"role": "planner", "output": "/workspace/results/planner.json"},
    )
    app_dsn, checkpoint_dsn = (
        os.environ["CONDERE_DATABASE_URL"],
        os.environ["CONDERE_CHECKPOINT_URL"],
    )
    app_options, checkpoint_options = conninfo_to_dict(app_dsn), conninfo_to_dict(checkpoint_dsn)
    assert app_options["user"] != checkpoint_options["user"]
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        with psycopg.connect(**(app_options | {"dbname": checkpoint_options["dbname"]})):
            pass
    with pytest.raises(ValueError, match="separate database"):
        async with checkpoint_store(app_dsn, application_dsn=app_dsn):
            pass

    graph = StateGraph(CheckpointState)
    graph.add_node("accept", lambda state: {"accepted": state["accepted"] + 1})
    graph.add_node("review", lambda state: {"reviewed": interrupt({"accepted": state["accepted"]})})
    graph.add_edge(START, "accept")
    graph.add_edge("accept", "review")
    graph.add_edge("review", END)
    config = {"configurable": {"thread_id": worker["thread_id"]}}
    async with checkpoint_store(checkpoint_dsn, application_dsn=app_dsn) as saver:
        interrupted = await graph.compile(checkpointer=saver).ainvoke(
            {"accepted": 0, "reviewed": False}, config
        )
        assert interrupted["accepted"] == 1
        assert interrupted["__interrupt__"]
    async with checkpoint_store(checkpoint_dsn, application_dsn=app_dsn) as saver:
        resumed = await graph.compile(checkpointer=saver).ainvoke(Command(resume=True), config)
        assert resumed == {"accepted": 1, "reviewed": True}
        assert (await saver.aget_tuple(config)).checkpoint["channel_values"]["reviewed"] is True
    assert store.operations(owner, case["id"]) == []


def test_money_and_original_identity_do_not_accept_floats_or_non_json():
    for invalid in (-1, 1.2, True):
        with pytest.raises(ValueError):
            money(invalid)
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        content_hash({"unknown": float("nan")})


def test_small_held_calls_close_at_their_full_reservation(store):
    owner, case = running(store)

    def lost():
        raise TimeoutError("simulated response loss")

    options = dict(
        owner=owner,
        assessment_id=case["id"],
        generation=case["generation"],
        kind="simulation",
        request={"prompt": "fixture"},
        dispatch_identity={"provider": "simulation"},
        invoke=lost,
    )
    for key, bound in (("small", 63_000), ("large", 400_000)):
        with pytest.raises(ProviderUncertain):
            execute_once(store, **(options | {"key": key, "max_microusd": bound}))
    [small] = store.settle_small_uncertain(owner, case["id"], 250_000)
    assert (small["state"], small["actual_microusd"]) == ("failed", 63_000)
    states = {item["operation_key"]: item["state"] for item in store.operations(owner, case["id"])}
    assert states == {"small": "failed", "large": "uncertain"}  # Larger calls still need review.


def test_provider_slots_are_shared_across_processes_and_expire(store):
    other = AssessmentStore(os.environ["CONDERE_DATABASE_URL"])  # stands in for another job
    provider = "test-provider-" + uuid4().hex
    entered = threading.Event()
    with store.provider_slot(provider, 1):

        def wait_for_slot():
            with other.provider_slot(provider, 1, poll_seconds=0.05):
                entered.set()

        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        time.sleep(0.5)
        assert not entered.is_set()  # the limit holds across separate stores
    waiter.join(timeout=5)
    assert entered.is_set()
    # A slot left by a lost process expires instead of blocking the provider forever.
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO provider_slots (holder, provider, acquired_at) "
            "VALUES (%s, %s, now() - interval '1 hour')",
            (provider + ":lost", provider),
        )
    with other.provider_slot(provider, 1, poll_seconds=0.05):
        pass
