"""One durable dispatch path for provider calls; never automatically retries.

The provider adapter must enforce the limits supporting its maximum reservation.
A reservation alone cannot constrain a provider's actual bill. All provider SDK
retries must be disabled by the adapter before it enters this wrapper.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from condere.runtime.state import AssessmentStore, Conflict, RecoveryRequired


@dataclass(frozen=True)
class ProviderResult:
    value: dict
    succeeded: bool = True
    actual_microusd: int | None = None
    cost_basis: dict | None = None


def execute_once(
    store: AssessmentStore,
    *,
    owner: str,
    assessment_id: str,
    generation: int,
    key: str,
    kind: str,
    request: dict,
    max_microusd: int,
    dispatch_identity: dict,
    invoke: Callable[[], ProviderResult],
) -> dict:
    operation = store.reserve_operation(
        owner, assessment_id, generation, key, kind, request, max_microusd
    )
    if operation["state"] in {"succeeded", "failed"}:
        return operation
    if operation["state"] != "reserved":
        raise RecoveryRequired("Existing dispatch has no final result; do not buy it again.")
    store.dispatch(owner, assessment_id, generation, operation["id"], dispatch_identity)
    try:
        result = invoke()
        return store.finish_operation(
            owner,
            assessment_id,
            operation["id"],
            result.value,
            succeeded=result.succeeded,
            actual_microusd=result.actual_microusd,
            cost_basis=result.cost_basis,
            reconciliation={
                "basis": "response observed for this dispatch",
                "operation_id": operation["id"],
            },
        )
    except BaseException:
        # A timeout, invalid response, or interrupted save is not proof of no bill.
        # Keep the dispatch identity and its reservation for explicit reconciliation.
        try:
            store.stop(owner, assessment_id, generation=generation)
        except Conflict:
            pass  # A stale worker must not interrupt newer, explicitly resumed work.
        raise


async def execute_once_async(store: AssessmentStore, **kwargs) -> dict:
    """Keep synchronous database/provider work off the API event loop."""
    return await asyncio.to_thread(execute_once, store, **kwargs)
