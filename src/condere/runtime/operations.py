"""One durable dispatch path for provider calls; never automatically retries.

The provider adapter must enforce the limits supporting its maximum reservation.
A reservation alone cannot constrain a provider's actual bill. All provider SDK
retries must be disabled by the adapter before it enters this wrapper.

A failed call ends only that call. A provider refusal received before any billable work
(`ProviderRefused`) is recorded as failed at $0. Any other failure may have billed: the
operation becomes uncertain with its error, keeps its reservation, and must be reconciled
before the assessment is delivered or continued (`ProviderUncertain`). Both reach the agent
as a failed tool or task, never as a silent retry. Losing the worker itself (cancellation,
interpreter exit) still stops the assessment.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from condere.runtime.state import AssessmentStore, Conflict


class ProviderRefused(Exception):
    """The provider answered with a refusal before starting billable work; nothing billed."""

    def __init__(self, provider: str, status: int | None, detail: str):
        super().__init__(f"{provider} refused the request ({status}): {detail[:300]}")
        self.provider, self.status, self.detail = provider, status, detail[:1_000]


class ProviderUncertain(Exception):
    """The call failed in a way that may have billed; its reservation awaits reconciliation."""


def refusal(provider: str, status: int, detail: str) -> ProviderRefused | None:
    """A client error (4xx other than a timeout) means the provider did no billable work."""
    return (
        ProviderRefused(provider, status, detail) if 400 <= status < 500 and status != 408 else None
    )


def previously_failed(provider: str, operation: dict) -> ProviderRefused:
    """A saved failure is final: the same request is never bought again."""
    detail = (operation.get("result") or {}).get("detail") or "earlier attempt failed"
    return ProviderRefused(provider, None, f"already failed and not bought again: {detail}")


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
        raise ProviderUncertain("This request has no final result yet; it is not bought again.")
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
    except RuntimeError as error:
        if "client has been closed" not in str(error):
            _hold(store, owner, assessment_id, operation, kind, error)
        # httpx raises this before sending anything (seen 2026-10-30 when a subagent call
        # started after its worker closed the client), so nothing can have been billed.
        refused = ProviderRefused(kind, None, f"not sent: {error}")
        _refuse(store, owner, assessment_id, operation, refused)
        raise refused from error
    except ProviderRefused as refused:
        _refuse(store, owner, assessment_id, operation, refused)
        raise
    except Exception as error:
        _hold(store, owner, assessment_id, operation, kind, error)
    except BaseException:
        # Losing the worker itself (cancellation, exit) stops the assessment as before.
        try:
            store.stop(owner, assessment_id, generation=generation)
        except Conflict:
            pass  # A stale worker must not interrupt newer, explicitly resumed work.
        raise


def _refuse(store, owner, assessment_id, operation, refused: ProviderRefused) -> None:
    store.finish_operation(
        owner,
        assessment_id,
        operation["id"],
        {"refused": refused.status, "detail": refused.detail},
        succeeded=False,
        actual_microusd=0,
        cost_basis={
            "basis": "provider refused or request not sent before any billable work",
            "status": refused.status,
        },
        reconciliation={"basis": "refusal observed for this dispatch"},
    )


def _hold(store, owner, assessment_id, operation, kind, error: Exception):
    """A timeout, invalid response, or interrupted save is not proof of no bill.

    Keep the dispatch identity and its reservation for explicit reconciliation.
    """
    detail = f"{type(error).__name__}: {str(error)[:500]}"
    store.mark_uncertain(owner, assessment_id, operation["id"], detail)
    raise ProviderUncertain(
        f"{kind} call failed and may have billed ({detail}); it is held for reconciliation "
        "and will not be bought again."
    ) from error


async def execute_once_async(store: AssessmentStore, **kwargs) -> dict:
    """Keep synchronous database/provider work off the API event loop."""
    return await asyncio.to_thread(execute_once, store, **kwargs)
