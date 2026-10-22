"""Authoritative assessment state and USD ledger; no provider or telemetry calls.

Money uses integer millionths of a dollar. The authenticated service supplies owner
and generation; neither is model authority. Campaign creation is an operator action.
All spending transitions lock the campaign before the assessment and operation.
"""

import hashlib
import json
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
    cap_microusd BIGINT NOT NULL CHECK (cap_microusd >= 0),
    assessment_limit INTEGER NOT NULL CHECK (assessment_limit > 0),
    authorization_basis JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    owner_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    brief JSONB NOT NULL, policy_version TEXT NOT NULL, method_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK
        (status IN ('admitted','running','interrupted','cancelled','completed')),
    generation INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, request_key)
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL REFERENCES assessments(id),
    operation_key TEXT NOT NULL, request_hash TEXT NOT NULL, kind TEXT NOT NULL,
    request JSONB NOT NULL, generation INTEGER NOT NULL,
    state TEXT NOT NULL CHECK
        (state IN ('reserved','dispatched','uncertain','succeeded','failed','released')),
    reserved_microusd BIGINT NOT NULL CHECK (reserved_microusd >= 0),
    actual_microusd BIGINT CHECK (actual_microusd >= 0),
    cost_basis JSONB, result JSONB, dispatch_identity JSONB, reconciliation JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), dispatched_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(assessment_id, operation_key)
);
CREATE TABLE IF NOT EXISTS records (
    assessment_id TEXT NOT NULL REFERENCES assessments(id), kind TEXT NOT NULL,
    record_key TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
    supersedes_hash TEXT, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(assessment_id, kind, record_key, version),
    UNIQUE(assessment_id, kind, record_key, content_hash)
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL REFERENCES assessments(id),
    worker_key TEXT NOT NULL, generation INTEGER NOT NULL, assignment JSONB NOT NULL,
    thread_id TEXT NOT NULL UNIQUE, machine_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('assigned','running','completed','interrupted')),
    completion JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(assessment_id, worker_key, generation)
);
CREATE INDEX IF NOT EXISTS operations_by_assessment ON operations(assessment_id);
CREATE INDEX IF NOT EXISTS assessments_by_campaign ON assessments(campaign_id);
"""


class StateError(Exception):
    """A safe, caller-visible state conflict; never includes credentials."""


class NotFound(StateError):
    pass


class Conflict(StateError):
    pass


class BudgetExceeded(StateError):
    pass


class RecoveryRequired(StateError):
    pass


def content_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def money(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("USD amounts must be nonnegative integer microdollars.")
    return value


class AssessmentStore:
    """Synchronous transactions; async adapters must use a worker thread."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            yield connection

    def setup(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(20261021, 4)")
            connection.execute(SCHEMA)

    def authorize_campaign(
        self,
        owner: str,
        campaign_id: str,
        cap_microusd: int,
        authorization: dict,
        *,
        assessment_limit: int = 1,
    ) -> dict:
        """Operator-only registration, never an intake field or agent tool."""
        money(cap_microusd)
        if not owner or not campaign_id or not authorization or assessment_limit < 1:
            raise ValueError("Campaign needs owner, identity, authorization, and a positive limit.")
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO campaigns
                   (id, owner_id, cap_microusd, assessment_limit, authorization_basis)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (campaign_id, owner, cap_microusd, assessment_limit, Jsonb(authorization)),
            )
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=%s", (campaign_id,)
            ).fetchone()
            if (
                campaign["owner_id"],
                campaign["cap_microusd"],
                campaign["assessment_limit"],
                campaign["authorization_basis"],
            ) != (owner, cap_microusd, assessment_limit, authorization):
                raise Conflict("Existing campaign authority cannot be replaced or enlarged.")
            return campaign

    @staticmethod
    def _assessment(connection, owner: str, assessment_id: str) -> dict:
        record = connection.execute(
            "SELECT * FROM assessments WHERE id=%s AND owner_id=%s", (assessment_id, owner)
        ).fetchone()
        if record is None:
            raise NotFound("Assessment not found.")
        return record

    @classmethod
    def _lock(cls, connection, owner: str, assessment_id: str) -> dict:
        record = cls._assessment(connection, owner, assessment_id)
        connection.execute(
            "SELECT id FROM campaigns WHERE id=%s FOR UPDATE", (record["campaign_id"],)
        )
        return connection.execute(
            "SELECT * FROM assessments WHERE id=%s FOR UPDATE", (assessment_id,)
        ).fetchone()

    @staticmethod
    def _active(assessment: dict, generation: int) -> None:
        if assessment["status"] != "running" or assessment["generation"] != generation:
            raise Conflict("Assessment is not running in this worker generation.")

    @staticmethod
    def _budget(connection, campaign_id: str) -> dict:
        totals = connection.execute(
            """SELECT COALESCE(sum(o.actual_microusd),0)::bigint AS settled_microusd,
                   COALESCE(sum(CASE WHEN o.actual_microusd IS NULL AND o.state <> 'released'
                       THEN o.reserved_microusd ELSE 0 END),0)::bigint AS pending_microusd
               FROM operations o JOIN assessments a ON a.id=o.assessment_id
               WHERE a.campaign_id=%s""",
            (campaign_id,),
        ).fetchone()
        cap = connection.execute(
            "SELECT cap_microusd FROM campaigns WHERE id=%s", (campaign_id,)
        ).fetchone()["cap_microusd"]
        return {"cap_microusd": cap, **totals, "available_microusd": cap - sum(totals.values())}

    def admit(
        self,
        owner: str,
        campaign_id: str,
        request_key: str,
        brief: dict,
        *,
        policy_version: str,
        method_version: str,
    ) -> dict:
        if not request_key or not brief or not policy_version or not method_version:
            raise ValueError(
                "Admission requires an original brief, request key, and policy/method versions."
            )
        digest = content_hash([campaign_id, brief, policy_version, method_version])
        with self.connection() as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=%s AND owner_id=%s FOR UPDATE",
                (campaign_id, owner),
            ).fetchone()
            if campaign is None:
                raise NotFound("Campaign not found.")
            existing = connection.execute(
                "SELECT * FROM assessments WHERE owner_id=%s AND request_key=%s",
                (owner, request_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise Conflict("Request key already belongs to different content.")
                return existing
            count = connection.execute(
                "SELECT count(*) AS n FROM assessments WHERE campaign_id=%s", (campaign_id,)
            ).fetchone()["n"]
            if count >= campaign["assessment_limit"]:
                raise Conflict("Campaign assessment allowance is already used.")
            try:
                return connection.execute(
                    """INSERT INTO assessments
                       (id,campaign_id,owner_id,request_key,request_hash,brief,policy_version,method_version,status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'admitted') RETURNING *""",
                    (
                        str(uuid4()),
                        campaign_id,
                        owner,
                        request_key,
                        digest,
                        Jsonb(brief),
                        policy_version,
                        method_version,
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise Conflict("Request key already belongs to another campaign.") from error

    def get(self, owner: str, assessment_id: str) -> dict:
        with self.connection() as connection:
            result = self._assessment(connection, owner, assessment_id)
            result["budget"] = self._budget(connection, result["campaign_id"])
            return result

    def begin(self, owner: str, assessment_id: str, *, resume: bool = False) -> dict:
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            allowed = {"interrupted", "cancelled"} if resume else {"admitted"}
            if assessment["status"] not in allowed:
                raise Conflict("Use explicit continuation for interrupted or cancelled work.")
            if connection.execute(
                """SELECT 1 FROM operations WHERE assessment_id=%s
                   AND state IN ('uncertain','dispatched') LIMIT 1""",
                (assessment_id,),
            ).fetchone():
                raise RecoveryRequired(
                    "Reconcile uncertain dispatched operations before continuing."
                )
            if self._budget(connection, assessment["campaign_id"])["available_microusd"] < 0:
                raise BudgetExceeded("Recorded liabilities exceed the original campaign allowance.")
            return connection.execute(
                """UPDATE assessments SET status='running',generation=generation+1,updated_at=now()
                   WHERE id=%s RETURNING *""",
                (assessment_id,),
            ).fetchone()

    @staticmethod
    def _stop(connection, assessment_id: str, status: str) -> None:
        connection.execute(
            """UPDATE assessments
               SET status=CASE WHEN status='cancelled' THEN 'cancelled' ELSE %s END,
               updated_at=now() WHERE id=%s""",
            (status, assessment_id),
        )
        connection.execute(
            """UPDATE operations SET state='released',actual_microusd=0,
                   cost_basis='{"basis":"never dispatched"}',updated_at=now()
               WHERE assessment_id=%s AND state='reserved'""",
            (assessment_id,),
        )
        connection.execute(
            """UPDATE operations SET state='uncertain',updated_at=now()
               WHERE assessment_id=%s AND state='dispatched'""",
            (assessment_id,),
        )
        connection.execute(
            """UPDATE workers SET status='interrupted'
               WHERE assessment_id=%s AND status IN ('assigned','running')""",
            (assessment_id,),
        )

    def stop(
        self,
        owner: str,
        assessment_id: str,
        *,
        cancel: bool = False,
        generation: int | None = None,
    ) -> dict:
        """Explicit cancellation or worker-loss handling; a client disconnect does not call this."""
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            if generation is not None and assessment["generation"] != generation:
                raise Conflict("A stale worker cannot interrupt a newer generation.")
            if assessment["status"] == "completed":
                raise Conflict("Completed work cannot be cancelled.")
            status = "cancelled" if cancel or assessment["status"] == "cancelled" else "interrupted"
            self._stop(connection, assessment_id, status)
            return self._assessment(connection, owner, assessment_id)

    def reserve_operation(
        self,
        owner: str,
        assessment_id: str,
        generation: int,
        operation_key: str,
        kind: str,
        request: dict,
        max_microusd: int,
    ) -> dict:
        money(max_microusd)
        if not operation_key or not kind:
            raise ValueError("Operation key and kind are required.")
        digest = content_hash([kind, request, max_microusd])
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            self._active(assessment, generation)
            existing = connection.execute(
                "SELECT * FROM operations WHERE assessment_id=%s AND operation_key=%s FOR UPDATE",
                (assessment_id, operation_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise Conflict(
                        "Operation key already belongs to different content or cost limit."
                    )
                if existing["state"] != "released":
                    return existing
            if (
                max_microusd
                > self._budget(connection, assessment["campaign_id"])["available_microusd"]
            ):
                raise BudgetExceeded("Reservation would exceed the campaign's remaining allowance.")
            if existing:
                return connection.execute(
                    """UPDATE operations SET state='reserved',generation=%s,actual_microusd=NULL,
                       cost_basis=NULL,updated_at=now() WHERE id=%s RETURNING *""",
                    (generation, existing["id"]),
                ).fetchone()
            return connection.execute(
                """INSERT INTO operations
                   (id,assessment_id,operation_key,request_hash,kind,request,generation,state,reserved_microusd)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'reserved',%s) RETURNING *""",
                (
                    str(uuid4()),
                    assessment_id,
                    operation_key,
                    digest,
                    kind,
                    Jsonb(request),
                    generation,
                    max_microusd,
                ),
            ).fetchone()

    @staticmethod
    def _operation(connection, assessment_id: str, operation_id: str) -> dict:
        operation = connection.execute(
            "SELECT * FROM operations WHERE id=%s AND assessment_id=%s FOR UPDATE",
            (operation_id, assessment_id),
        ).fetchone()
        if operation is None:
            raise NotFound("Operation not found.")
        return operation

    def dispatch(
        self, owner: str, assessment_id: str, generation: int, operation_id: str, identity: dict
    ) -> dict:
        """Commit before the external side effect. Only one caller can win dispatch."""
        if not identity:
            raise ValueError("Dispatch needs a persisted provider/request identity.")
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            self._active(assessment, generation)
            operation = self._operation(connection, assessment_id, operation_id)
            if operation["state"] != "reserved" or operation["generation"] != generation:
                raise Conflict(
                    "Operation cannot dispatch again; retrieve or reconcile its saved state."
                )
            if self._budget(connection, assessment["campaign_id"])["available_microusd"] < 0:
                raise BudgetExceeded("Pending and settled costs exceed the campaign allowance.")
            return connection.execute(
                """UPDATE operations SET state='dispatched',dispatch_identity=%s,
                   dispatched_at=now(),updated_at=now()
                   WHERE id=%s RETURNING *""",
                (Jsonb(identity), operation_id),
            ).fetchone()

    def finish_operation(
        self,
        owner: str,
        assessment_id: str,
        operation_id: str,
        result: dict,
        *,
        succeeded: bool,
        actual_microusd: int | None = None,
        cost_basis: dict | None = None,
        reconciliation: dict | None = None,
    ) -> dict:
        if actual_microusd is not None:
            money(actual_microusd)
            if not cost_basis:
                raise ValueError("Known charges, including zero, require a cost basis.")
        elif cost_basis is not None:
            raise ValueError(
                "Unknown charges must keep their reservation, not a claimed cost basis."
            )
        state = "succeeded" if succeeded else "failed"
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            operation = self._operation(connection, assessment_id, operation_id)
            if operation["state"] in {"succeeded", "failed"}:
                if (
                    operation["state"],
                    operation["result"],
                    operation["actual_microusd"],
                    operation["cost_basis"],
                ) != (state, result, actual_microusd, cost_basis):
                    raise Conflict("A saved operation result cannot be replaced.")
                return operation
            if operation["state"] == "uncertain" and not reconciliation:
                raise RecoveryRequired("Uncertain operations need reconciliation evidence.")
            if operation["state"] not in {"dispatched", "uncertain"}:
                raise Conflict("Only dispatched operations can receive a provider result.")
            saved = connection.execute(
                """UPDATE operations SET state=%s,result=%s,actual_microusd=%s,cost_basis=%s,
                   reconciliation=%s,updated_at=now() WHERE id=%s RETURNING *""",
                (
                    state,
                    Jsonb(result),
                    actual_microusd,
                    Jsonb(cost_basis) if cost_basis else None,
                    Jsonb(reconciliation) if reconciliation else None,
                    operation_id,
                ),
            ).fetchone()
            if self._budget(connection, assessment["campaign_id"])["available_microusd"] < 0:
                self._stop(connection, assessment_id, "interrupted")
            return saved

    def settle_cost(
        self, owner: str, assessment_id: str, operation_id: str, actual_microusd: int, basis: dict
    ) -> dict:
        """Resolve an unknown bill without replacing the original provider result."""
        money(actual_microusd)
        if not basis:
            raise ValueError(
                "Settlement requires provider evidence or an explicit calculation basis."
            )
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            operation = self._operation(connection, assessment_id, operation_id)
            if operation["state"] not in {"succeeded", "failed"}:
                raise RecoveryRequired("Reconcile the operation outcome before settling its bill.")
            if operation["actual_microusd"] is not None:
                if (operation["actual_microusd"], operation["cost_basis"]) != (
                    actual_microusd,
                    basis,
                ):
                    raise Conflict("A settled cost cannot be silently rewritten.")
                return operation
            saved = connection.execute(
                """UPDATE operations SET actual_microusd=%s,cost_basis=%s,updated_at=now()
                   WHERE id=%s RETURNING *""",
                (actual_microusd, Jsonb(basis), operation_id),
            ).fetchone()
            if self._budget(connection, assessment["campaign_id"])["available_microusd"] < 0:
                self._stop(connection, assessment_id, "interrupted")
            return saved

    def operations(self, owner: str, assessment_id: str) -> list[dict]:
        with self.connection() as connection:
            self._assessment(connection, owner, assessment_id)
            return connection.execute(
                "SELECT * FROM operations WHERE assessment_id=%s ORDER BY created_at,id",
                (assessment_id,),
            ).fetchall()

    def append_record(
        self,
        owner: str,
        assessment_id: str,
        kind: str,
        key: str,
        payload: dict,
        *,
        supersedes_hash: str | None = None,
    ) -> dict:
        """Keep originals immutable; a revision explicitly names the prior accepted version."""
        if not kind or not key:
            raise ValueError("Record kind and key are required.")
        digest = content_hash(payload)
        with self.connection() as connection:
            self._lock(connection, owner, assessment_id)
            old = connection.execute(
                """SELECT * FROM records WHERE assessment_id=%s AND kind=%s AND record_key=%s
                   ORDER BY version DESC""",
                (assessment_id, kind, key),
            ).fetchall()
            for record in old:
                if record["content_hash"] == digest:
                    return record
            if supersedes_hash != (old[0]["content_hash"] if old else None):
                raise Conflict(
                    "Revision must name the latest accepted original; originals remain retrievable."
                )
            return connection.execute(
                """INSERT INTO records
                   (assessment_id,kind,record_key,version,content_hash,supersedes_hash,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (assessment_id, kind, key, len(old) + 1, digest, supersedes_hash, Jsonb(payload)),
            ).fetchone()

    def records(self, owner: str, assessment_id: str, *, kind: str | None = None) -> list[dict]:
        with self.connection() as connection:
            self._assessment(connection, owner, assessment_id)
            return connection.execute(
                """SELECT * FROM records WHERE assessment_id=%s AND (%s::text IS NULL OR kind=%s)
                   ORDER BY kind,record_key,version""",
                (assessment_id, kind, kind),
            ).fetchall()

    def assign_worker(
        self, owner: str, assessment_id: str, generation: int, key: str, assignment: dict
    ) -> dict:
        if not key or not assignment:
            raise ValueError("Worker needs a key and an explicit assignment.")
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            self._active(assessment, generation)
            worker_id = str(uuid4())
            connection.execute(
                """INSERT INTO workers
                   (id,assessment_id,worker_key,generation,assignment,thread_id,status)
                   VALUES (%s,%s,%s,%s,%s,%s,'assigned') ON CONFLICT DO NOTHING""",
                (
                    worker_id,
                    assessment_id,
                    key,
                    generation,
                    Jsonb(assignment),
                    assessment_id + ":" + worker_id,
                ),
            )
            worker = connection.execute(
                "SELECT * FROM workers WHERE assessment_id=%s AND worker_key=%s AND generation=%s",
                (assessment_id, key, generation),
            ).fetchone()
            if worker["assignment"] != assignment:
                raise Conflict("Existing worker assignment cannot be replaced.")
            return worker

    def attach_machine(
        self, owner: str, assessment_id: str, generation: int, worker_id: str, machine_id: str
    ) -> dict:
        if not machine_id:
            raise ValueError("A durable machine identity is required.")
        with self.connection() as connection:
            assessment = self._lock(connection, owner, assessment_id)
            self._active(assessment, generation)
            worker = connection.execute(
                """SELECT * FROM workers WHERE id=%s AND assessment_id=%s
                   AND generation=%s FOR UPDATE""",
                (worker_id, assessment_id, generation),
            ).fetchone()
            if worker is None:
                raise NotFound("Worker not found.")
            if worker["machine_id"] not in {None, machine_id} or worker["status"] not in {
                "assigned",
                "running",
            }:
                raise Conflict("Worker machine identity cannot be replaced.")
            return connection.execute(
                "UPDATE workers SET machine_id=%s,status='running' WHERE id=%s RETURNING *",
                (machine_id, worker_id),
            ).fetchone()

    def workers(self, owner: str, assessment_id: str) -> list[dict]:
        with self.connection() as connection:
            self._assessment(connection, owner, assessment_id)
            return connection.execute(
                "SELECT * FROM workers WHERE assessment_id=%s ORDER BY created_at,id",
                (assessment_id,),
            ).fetchall()
