"""One bounded assessment: Technical/Market work, Business synthesis, and final review."""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

from langfuse import Langfuse
from smol import ConnectOptions, Machine

from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.contracts import CAPABILITY_CATEGORIES
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
from condere.runtime.workers import run_worker

STAGES = (("technical", "market"), ("business",), ("review",))


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
        exa_key: str | None = None,
        model_factory=None,
        callbacks: list | None = None,
        telemetry: Langfuse | None = None,
    ):
        if service.execution_mode == "live" and (
            not openrouter_key or not exa_key or model_factory
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
        self._model_factory, self.callbacks = model_factory, callbacks or []
        self.telemetry = telemetry
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    @staticmethod
    def machine_name(worker: dict) -> str:
        return "condere-" + worker["id"]

    def _plan(self, owner, assessment_id, brief, requirements, private_terms):
        prior = self.state.records(owner, assessment_id, kind="execution_plan")
        if prior:
            plan = prior[0]["payload"]
            if requirements is not None and requirements != plan["required_capabilities"]:
                raise Conflict("Continuation cannot replace the admitted capability requirements.")
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
        requirements = (
            requirements
            if requirements is not None
            else [
                {"scenario": "primary", "category": category, "name": category}
                for category in sorted(CAPABILITY_CATEGORIES)
            ]
        )
        if any(
            set(item) != {"scenario", "category", "name"}
            or item["category"] not in CAPABILITY_CATEGORIES
            or not item["scenario"]
            or not item["name"]
            for item in requirements
        ):
            raise ValueError("Every capability requirement needs its scenario, category, and name.")
        plan = {
            "stages": [list(stage) for stage in STAGES],
            "repository_candidates": candidates,
            "required_capabilities": requirements,
            "private_terms": list(private_terms or ()),
        }
        self.state.append_record(owner, assessment_id, "execution_plan", "main", plan)
        return plan

    def _completed_role(self, owner, assessment_id, role):
        if role == "review":
            return (
                None  # A continuation reviews the currently accepted findings and saved evidence.
            )
        heads = self.service._heads(self.state.records(owner, assessment_id))
        exports = {
            item["record_key"]: item["payload"]
            for item in self.state.records(owner, assessment_id, kind="worker_export")
        }
        for worker in reversed(self.state.workers(owner, assessment_id)):
            if worker["assignment"]["role"] != role or worker["status"] != "completed":
                continue
            accepted = (worker["completion"] or {}).get("accepted_hashes", {})
            exported = exports.get(worker["id"])
            if exported is None or exported.get("input_hashes") != self._input_hashes(heads, role):
                continue
            if accepted and all(
                heads.get(key, {}).get("content_hash") == value for key, value in accepted.items()
            ):
                return worker
        return None

    @staticmethod
    def _input_hashes(heads, role):
        return {
            key: record["content_hash"]
            for key, record in heads.items()
            if role in {"business", "review"}
            and (
                key == "technical:main"
                or key.startswith("question:M")
                or (role == "review" and key.startswith("question:B"))
            )
        }

    async def _stop_machines(self, owner, assessment_id):
        workers = await asyncio.to_thread(self.state.workers, owner, assessment_id)
        names = {worker["machine_id"] or self.machine_name(worker): worker for worker in workers}
        machines = await asyncio.to_thread(Machine.list, ConnectOptions(target="local"))
        for machine in machines:
            if machine.id not in names or machine.state in {"stopped", "exited"}:
                continue
            worker = names[machine.id]
            sandbox = await asyncio.to_thread(
                SmolSandbox.reconnect, machine.id, self.artifacts / assessment_id / worker["id"]
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
        current = await asyncio.to_thread(self.state.get, owner, assessment_id)
        if self.telemetry is None or current["status"] == "completed":
            return await self._run(owner, assessment_id, **options)
        span_context = contextlib.ExitStack()
        trace_id = None
        try:
            span_context.enter_context(
                self.telemetry.start_as_current_observation(
                    name="condere.assessment",
                    metadata={
                        "assessment_id": assessment_id,
                        "execution_mode": self.service.execution_mode,
                    },
                )
            )
            trace_id = self.telemetry.get_current_trace_id()
        except Exception:
            logging.getLogger(__name__).warning("Assessment telemetry could not start.")
        try:
            return await self._run(owner, assessment_id, **options)
        finally:
            try:
                span_context.close()
                await asyncio.to_thread(self._record_telemetry, owner, assessment_id, trace_id)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Assessment telemetry status could not be saved."
                )

    def _record_telemetry(self, owner: str, assessment_id: str, trace_id: str | None):
        status, error_type = "unavailable", None
        try:
            if trace_id:
                self.telemetry.flush()
                observations = self.telemetry.api.observations.get_many(
                    trace_id=trace_id,
                    fields="core",
                    limit=1,
                    request_options={"timeout_in_seconds": 2, "max_retries": 0},
                )
                status = "observed" if observations.data else "pending"
        except Exception as error:
            error_type = type(error).__name__
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
            },
        )

    async def _run(
        self,
        owner: str,
        assessment_id: str,
        *,
        resume: bool = False,
        required_capabilities: list[dict] | None = None,
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
                self._plan,
                owner,
                assessment_id,
                current["brief"],
                required_capabilities,
                private_terms,
            )
            case = await asyncio.to_thread(self.service.begin, owner, assessment_id, resume=resume)
            generation = case["generation"]
            self._tasks[(owner, assessment_id)] = asyncio.current_task()
            try:
                async with checkpoint_store(
                    self.checkpoint_dsn, application_dsn=self.application_dsn
                ) as checkpointer:
                    for stage in plan["stages"]:
                        async with asyncio.TaskGroup() as tasks:
                            for role in stage:
                                tasks.create_task(
                                    self._run_role(
                                        owner, assessment_id, generation, role, plan, checkpointer
                                    )
                                )
                return await asyncio.to_thread(
                    self.service.finalize, owner, assessment_id, generation
                )
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

    async def _run_role(self, owner, assessment_id, generation, role, plan, checkpointer):
        previous = await asyncio.to_thread(self._completed_role, owner, assessment_id, role)
        if previous is not None:
            return previous
        requirements = plan["required_capabilities"] if role == "technical" else None
        worker = await asyncio.to_thread(
            self.service.assign_worker,
            owner,
            assessment_id,
            generation,
            role,
            required_capabilities=requirements,
        )
        artifact_dir = self.artifacts / assessment_id / worker["id"]
        inputs = self._input_hashes(
            self.service._heads(await asyncio.to_thread(self.state.records, owner, assessment_id)),
            role,
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
            context = ProviderContext(self.state, owner, assessment_id, generation, role)
            model = (
                self._model_factory(context)
                if self._model_factory
                else accounted_model(context, self._router_key)
            )
            if role != "review" and self.service.execution_mode == "live":
                research = ExaResearch(
                    context,
                    self.service.evidence,
                    self._exa_key,
                    private_terms=tuple(plan["private_terms"]),
                )
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
                required_capabilities=requirements,
                callbacks=self.callbacks,
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
            if isinstance(model, AccountedChatOpenAI):
                await asyncio.to_thread(model.root_client.close)
                await model.root_async_client.close()
            await asyncio.to_thread(sandbox.close, delete=succeeded)
