"""One assessment: repository index, per-question Technical/Market/Business work, review."""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

import httpx
from smol import ConnectOptions, Machine

from condere.runtime.chatgpt_plan import PlanChatOpenAI, PlanCredentials, plan_model
from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.contracts import BUSINESS_IDS, MARKET_IDS, QUESTIONS, TECHNICAL_IDS
from condere.runtime.indexing import (
    build_index,
    github_metadata,
    install_index,
    pinned_snapshot,
    save_index,
    save_metadata,
)
from condere.runtime.providers import (
    AccountedChatOpenAI,
    ExaResearch,
    ProviderContext,
    accounted_model,
)
from condere.runtime.repositories import canonical_repository
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import Conflict
from condere.runtime.tracing import Tracing, WorkerTrace
from condere.runtime.workers import run_worker

# Technical analysis of the code comes first; T9 synthesizes T1-T8 before Market and Business.
STAGE_ROLES = {"technical", "market", "business"}
STAGES = (
    tuple("technical:" + identity for identity in TECHNICAL_IDS if identity != "T9"),
    ("technical:T9",),
    tuple("market:" + identity for identity in MARKET_IDS),
    tuple("business:" + identity for identity in BUSINESS_IDS),
    ("review",),
)


class AssessmentCoordinator:
    def __init__(
        self,
        service: AssessmentService,
        *,
        checkpoint_dsn: str,
        application_dsn: str,
        image: str,
        artifacts: Path,
        openrouter_key: str | None = None,
        plan_credentials: PlanCredentials | None = None,
        exa_key: str | None = None,
        model_factory=None,
        callbacks: list | None = None,
        tracing: Tracing | None = None,
        concurrency: int = 3,
    ):
        if service.execution_mode == "live" and (
            not (openrouter_key or plan_credentials) or not exa_key or model_factory
        ):
            raise ValueError(
                "Live coordination requires provider keys and the accounted model factory."
            )
        if service.execution_mode == "simulation" and model_factory is None:
            raise ValueError("Simulation requires an explicit non-provider model factory.")
        self.service, self.state = service, service.state
        self.checkpoint_dsn, self.application_dsn = checkpoint_dsn, application_dsn
        self.image, self.artifacts = image, artifacts.resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._router_key, self._exa_key = openrouter_key, exa_key
        # ChatGPT plan usage takes precedence when the operator signed in for it.
        self.plan_credentials = plan_credentials
        self._model_factory, self.callbacks = model_factory, callbacks or []
        self.tracing = tracing
        if concurrency < 1:
            raise ValueError("Run at least one worker at a time.")
        self.concurrency = concurrency
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    @staticmethod
    def machine_name(worker: dict) -> str:
        return "condere-" + worker["id"]

    def _plan(self, owner, assessment_id, brief, private_terms):
        prior = self.state.records(owner, assessment_id, kind="execution_plan")
        if prior:
            plan = prior[0]["payload"]
            if private_terms is not None and list(private_terms) != plan["private_terms"]:
                raise Conflict("Continuation must preserve the admitted private-query terms.")
            return plan
        references = "\n".join([brief["original_input"], *brief.get("public_references", [])])
        candidates = sorted(
            {
                canonical_repository(url.rstrip(".,"))
                for url in re.findall(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", references
                )
            }
        )
        plan = {
            "stages": ([["screen"]] if brief.get("analysis_depth") == "adaptive" else [])
            + [list(stage) for stage in STAGES],
            "repository_candidates": candidates,
            "private_terms": list(private_terms or ()),
        }
        self.state.append_record(owner, assessment_id, "execution_plan", "main", plan)
        return plan

    def _completed_unit(self, owner, assessment_id, unit):
        # A completed review is reused only while every reviewed answer is unchanged (input
        # hashes below), like any other unit; finalization verifies the hashes again.
        heads = self.service._heads(self.state.records(owner, assessment_id))
        exports = {
            item["record_key"]: item["payload"]
            for item in self.state.records(owner, assessment_id, kind="worker_export")
        }
        for worker in reversed(self.state.workers(owner, assessment_id)):
            if worker["worker_key"] != unit or worker["status"] != "completed":
                continue
            accepted = (worker["completion"] or {}).get("accepted_hashes", {})
            exported = exports.get(worker["id"])
            if exported is None or exported.get("input_hashes") != self._input_hashes(heads, unit):
                continue
            if accepted and all(
                heads.get(key, {}).get("content_hash") == value for key, value in accepted.items()
            ):
                return worker
        return None

    @staticmethod
    def _input_hashes(heads, unit):
        """Accepted records a unit builds on; a changed input invalidates its saved result."""
        role, _, question = unit.partition(":")
        prefixes = {
            "screen": (),
            "technical": ("question:T",) if question == "T9" else (),
            "market": ("question:T",),
            "business": ("question:T", "question:M"),
            "review": ("question:",),
        }[role]
        return {
            key: record["content_hash"]
            for key, record in heads.items()
            if (role != "screen" and key == "screen:main")
            or (key.startswith(prefixes) and key != "question:" + question)
        }

    async def _stop_machines(self, owner, assessment_id):
        workers = await asyncio.to_thread(self.state.workers, owner, assessment_id)
        names = {worker["machine_id"] or self.machine_name(worker): worker for worker in workers}
        machines = await asyncio.to_thread(Machine.list, ConnectOptions(target="local"))
        for machine in machines:
            report_machine = re.fullmatch(
                rf"condere-(report|index)-{re.escape(assessment_id)}-[0-9]+", machine.id
            )
            if (machine.id not in names and not report_machine) or machine.state in {
                "stopped",
                "exited",
            }:
                continue
            artifact = (
                names[machine.id]["id"]
                if machine.id in names
                else machine.id.split("-")[1] + "-" + machine.id.rsplit("-", 1)[1]
            )
            sandbox = await asyncio.to_thread(
                SmolSandbox.reconnect, machine.id, self.artifacts / assessment_id / artifact
            )
            await asyncio.to_thread(sandbox.close)

    async def cancel(self, owner: str, assessment_id: str) -> dict:
        before = await asyncio.to_thread(self.state.get, owner, assessment_id)
        await asyncio.to_thread(self.state.stop, owner, assessment_id, cancel=True)
        task = self._tasks.get((owner, assessment_id))
        if before["status"] != "cancelled" and task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_machines(owner, assessment_id)
        return await asyncio.to_thread(self.state.get, owner, assessment_id)

    async def run(
        self,
        owner: str,
        assessment_id: str,
        **options,
    ):
        return await self._run(owner, assessment_id, **options)

    def _record_telemetry(self, owner, assessment_id, worker, trace_id: str | None):
        status, error_type = (
            self.tracing.status(trace_id) if trace_id else ("unavailable", "TraceNotStarted")
        )
        self.state.append_record(
            owner,
            assessment_id,
            "telemetry",
            trace_id or str(uuid4()),
            {
                "trace_id": trace_id,
                "status": status,
                "error_type": error_type,
                "execution_mode": self.service.execution_mode,
                "worker_id": worker["id"],
                "role": worker["assignment"]["role"],
            },
        )

    async def _run(
        self,
        owner: str,
        assessment_id: str,
        *,
        resume: bool = False,
        private_terms: tuple[str, ...] | None = None,
    ):
        async with self.state.execution_lock(owner, assessment_id):
            current = await asyncio.to_thread(self.state.get, owner, assessment_id)
            if current["status"] == "completed":
                return await asyncio.to_thread(self.service.report, owner, assessment_id)
            if current["status"] == "running" and resume:
                # Acquiring the session lock establishes that the previous coordinator is gone.
                await asyncio.to_thread(
                    self.state.stop, owner, assessment_id, generation=current["generation"]
                )
            if resume:
                await self._stop_machines(owner, assessment_id)
            plan = await asyncio.to_thread(
                self._plan, owner, assessment_id, current["brief"], private_terms
            )
            case = await asyncio.to_thread(self.service.begin, owner, assessment_id, resume=resume)
            generation = case["generation"]
            self._tasks[(owner, assessment_id)] = asyncio.current_task()
            try:
                await self._index(owner, assessment_id, generation, plan)
                slots = asyncio.Semaphore(self.concurrency)

                async def bounded(unit, checkpointer):
                    async with slots:
                        return await self._run_unit(
                            owner, assessment_id, generation, unit, plan, checkpointer
                        )

                async with checkpoint_store(
                    self.checkpoint_dsn, application_dsn=self.application_dsn
                ) as checkpointer:
                    for stage in plan["stages"]:
                        # Let siblings finish when one unit fails: cancelling them mid-call
                        # strands their dispatches and discards their progress.
                        outcomes = await asyncio.gather(
                            *(bounded(unit, checkpointer) for unit in stage),
                            return_exceptions=True,
                        )
                        failures = [item for item in outcomes if isinstance(item, BaseException)]
                        if failures:
                            raise BaseExceptionGroup("Assessment stage did not complete.", failures)
                        if stage == ["screen"]:
                            screen = await asyncio.to_thread(
                                self.service.screening, owner, assessment_id
                            )
                            if screen.recommendation != "deepen":
                                return await asyncio.to_thread(
                                    self.service.finalize_screen, owner, assessment_id, generation
                                )
                creation = asyncio.create_task(
                    asyncio.to_thread(
                        SmolSandbox.create,
                        f"condere-report-{assessment_id}-{generation}",
                        self.image,
                        self.artifacts / assessment_id / f"report-{generation}",
                    )
                )
                try:
                    report_sandbox = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    report_sandbox = await creation
                    await asyncio.to_thread(report_sandbox.close)
                    raise
                rendered = False
                try:
                    dossier = await asyncio.to_thread(
                        self.service.finalize,
                        owner,
                        assessment_id,
                        generation,
                        sandbox=report_sandbox,
                    )
                    rendered = True
                    return dossier
                finally:
                    await asyncio.to_thread(report_sandbox.close, delete=rendered)
            except BaseException:
                try:
                    await asyncio.to_thread(
                        self.state.stop, owner, assessment_id, generation=generation
                    )
                except Conflict:
                    pass
                raise
            finally:
                self._tasks.pop((owner, assessment_id), None)

    def _worker_trace(self, worker, assessment_id, generation, brief, indexes):
        if self.tracing is None:
            return contextlib.nullcontext(WorkerTrace())
        assignment = worker["assignment"]
        question = assignment["question_ids"][0] if assignment["role"] in STAGE_ROLES else None
        return self.tracing.worker(
            "condere." + assignment["role"],
            inputs={
                "brief": brief,
                "question_id": question,
                "question": QUESTIONS[question]["question"] if question else None,
                "repositories": [
                    {"repository": item["repository"], "commit": item["commit"]} for item in indexes
                ],
            },
            metadata={
                "assessment_id": assessment_id,
                "worker_id": worker["id"],
                "generation": generation,
                "role": assignment["role"],
                "question_id": question,
                "skills": assignment["skills"],
                "execution_mode": self.service.execution_mode,
            },
            tags=["condere", assignment["role"], question or assignment["role"]],
        )

    async def _index(self, owner, assessment_id, generation, plan):
        """Deterministically index each admitted repository once, before any model work."""
        indexed = {
            item["payload"]["repository"]
            for item in await asyncio.to_thread(
                self.state.records, owner, assessment_id, kind="repository_index"
            )
        }
        pending = [url for url in plan["repository_candidates"][:3] if url not in indexed]
        if not pending:
            return
        creation = asyncio.create_task(
            asyncio.to_thread(
                SmolSandbox.create,
                f"condere-index-{assessment_id}-{generation}",
                self.image,
                self.artifacts / assessment_id / f"index-{generation}",
            )
        )
        try:
            sandbox = await asyncio.shield(creation)
        except asyncio.CancelledError:
            sandbox = await creation
            await asyncio.to_thread(sandbox.close)
            raise
        succeeded = False
        try:
            for url in pending:
                snapshot = await asyncio.to_thread(
                    pinned_snapshot,
                    self.state,
                    owner,
                    assessment_id,
                    generation,
                    url,
                    {"basis": "reference supplied in the admitted brief"},
                    self.artifacts / assessment_id / "repositories",
                )
                files = await asyncio.to_thread(build_index, snapshot, sandbox)
                record = await asyncio.to_thread(
                    save_index, self.service.evidence, owner, assessment_id, snapshot, files
                )
                try:
                    metadata = await asyncio.to_thread(github_metadata, url)
                    record["metadata_source_id"] = await asyncio.to_thread(
                        save_metadata, self.service.evidence, owner, assessment_id, metadata
                    )
                except httpx.HTTPError as error:
                    record["metadata_limitation"] = (
                        f"GitHub metadata unavailable ({type(error).__name__})."
                    )
                await asyncio.to_thread(
                    self.state.append_record,
                    owner,
                    assessment_id,
                    "repository_index",
                    snapshot.identity,
                    record,
                )
            succeeded = True
        finally:
            await asyncio.to_thread(sandbox.close, delete=succeeded)

    async def _run_unit(self, owner, assessment_id, generation, unit, plan, checkpointer):
        previous = await asyncio.to_thread(self._completed_unit, owner, assessment_id, unit)
        if previous is not None:
            return previous
        role, _, question = unit.partition(":")
        worker = await asyncio.to_thread(
            self.service.assign_worker,
            owner,
            assessment_id,
            generation,
            role,
            question or None,
        )
        artifact_dir = self.artifacts / assessment_id / worker["id"]
        inputs = self._input_hashes(
            self.service._heads(await asyncio.to_thread(self.state.records, owner, assessment_id)),
            unit,
        )
        creation = asyncio.create_task(
            asyncio.to_thread(
                SmolSandbox.create, self.machine_name(worker), self.image, artifact_dir
            )
        )
        try:
            sandbox = await asyncio.shield(creation)
        except asyncio.CancelledError:
            sandbox = await creation
            await asyncio.to_thread(sandbox.close)
            raise
        succeeded, research, model = False, None, None
        try:
            if sandbox.id != self.machine_name(worker):
                raise Conflict(
                    "The native VM identity differs from the recorded worker allocation."
                )
            indexes = [
                record["payload"]
                for record in await asyncio.to_thread(
                    self.state.records, owner, assessment_id, kind="repository_index"
                )
            ]
            if role != "review":
                for record in indexes:
                    await asyncio.to_thread(
                        install_index, self.service.evidence, owner, assessment_id, record, sandbox
                    )
            brief = (await asyncio.to_thread(self.state.get, owner, assessment_id))["brief"]
            context = ProviderContext(
                self.state, owner, assessment_id, generation, role, question or None
            )
            model = (
                self._model_factory(context)
                if self._model_factory
                else plan_model(context, self.plan_credentials)
                if self.plan_credentials is not None
                else accounted_model(context, self._router_key)
            )
            if role != "review" and self.service.execution_mode == "live":
                research = ExaResearch(
                    context,
                    self.service.evidence,
                    self._exa_key,
                    private_terms=tuple(plan["private_terms"]),
                )
            worker_trace = WorkerTrace()
            try:
                with self._worker_trace(
                    worker, assessment_id, generation, brief, indexes
                ) as worker_trace:
                    completed = await run_worker(
                        self.service,
                        owner=owner,
                        assessment_id=assessment_id,
                        generation=generation,
                        role=role,
                        sandbox=sandbox,
                        model=model,
                        checkpointer=checkpointer,
                        research=research,
                        repository_cache=self.artifacts / assessment_id / "repositories",
                        allowed_repositories=set(plan["repository_candidates"]),
                        question_id=question or None,
                        callbacks=self.callbacks,
                    )
                    accepted = self.service._heads(
                        await asyncio.to_thread(self.state.records, owner, assessment_id)
                    )
                    answer = {
                        key: accepted[key]["payload"]
                        for key in completed["completion"]["accepted_hashes"]
                    }
                    worker_trace.outputs(answer)
                    if (
                        self.tracing is not None
                        and role in STAGE_ROLES
                        and self.service.execution_mode == "live"
                    ):
                        await asyncio.to_thread(self.tracing.record_example, worker_trace, answer)
            finally:
                # Check presence after the root run closes; a missing trace is never a zero charge.
                if self.tracing is not None:
                    try:
                        await asyncio.to_thread(
                            self._record_telemetry,
                            owner,
                            assessment_id,
                            worker,
                            worker_trace.trace_id,
                        )
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "Worker telemetry status could not be saved."
                        )
            exported = await asyncio.to_thread(
                sandbox.download_files, [worker["assignment"]["output_path"]]
            )
            if exported[0].error or exported[0].content is None:
                raise RuntimeError(
                    "Accepted worker output could not be exported before VM cleanup."
                )
            heads = self.service._heads(
                await asyncio.to_thread(self.state.records, owner, assessment_id)
            )
            expected = {
                key: {"content_hash": digest, "payload": heads[key]["payload"]}
                for key, digest in completed["completion"]["accepted_hashes"].items()
            }
            if json.loads(exported[0].content) != expected:
                raise Conflict("The VM output differs from the accepted findings.")
            output = artifact_dir / "accepted.json"
            with output.open("xb") as target:
                target.write(exported[0].content)
            output.chmod(0o600)
            await asyncio.to_thread(
                self.state.append_record,
                owner,
                assessment_id,
                "worker_export",
                worker["id"],
                {
                    "worker_id": worker["id"],
                    "machine_id": sandbox.id,
                    "accepted_hashes": completed["completion"]["accepted_hashes"],
                    "input_hashes": inputs,
                    "sha256": hashlib.sha256(exported[0].content).hexdigest(),
                },
            )
            succeeded = True
            return completed
        finally:
            if research is not None:
                research.close()
            if isinstance(model, (AccountedChatOpenAI, PlanChatOpenAI)):
                await asyncio.to_thread(model.root_client.close)
                await model.root_async_client.close()
            await asyncio.to_thread(sandbox.close, delete=succeeded)
