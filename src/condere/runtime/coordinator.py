"""One assessment: repository index, the lead analysis, then the Jev decision."""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

import httpx

from condere.runtime.chatgpt_plan import (
    PLAN_CODE_MODEL,
    PLAN_EXTRACT_MODEL,
    PlanChatOpenAI,
    PlanCredentials,
    plan_model,
)
from condere.runtime.checkpoints import checkpoint_store
from condere.runtime.decision import decide
from condere.runtime.evidence import EvidenceError
from condere.runtime.indexing import (
    build_index,
    github_metadata,
    install_index,
    pinned_snapshot,
    save_index,
    save_metadata,
)
from condere.runtime.providers import (
    CLAUDE_HAIKU,
    CLAUDE_OPUS,
    AccountedChatOpenAI,
    ApifyResearch,
    CommunityResearch,
    ExaResearch,
    JevDecisions,
    ProviderContext,
    accounted_model,
)
from condere.runtime.repositories import canonical_repository
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import Conflict, NotFound
from condere.runtime.tracing import Tracing, WorkerTrace
from condere.runtime.workers import run_worker

# Held calls reserved at or below this are closed at delivery or continuation, charged in full
# (owner decision 2026-10-30); larger ones still need explicit reconciliation.
SMALL_CHARGE_MICROUSD = 250_000


async def run_recorded(coordinator, state, owner: str, assessment_id: str, *, resume=False):
    """Run an assessment; when it stops on an error, save the error types for its status.

    Shared by the in-process API path and the one-job-per-assessment cloud path.
    """
    try:
        await coordinator.run(owner, assessment_id, resume=resume)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        current = await asyncio.to_thread(state.get, owner, assessment_id)
        if current["status"] != "running":
            pending, error_types = [error], set()
            while pending:
                item = pending.pop()
                if isinstance(item, BaseExceptionGroup):
                    pending.extend(item.exceptions)
                else:
                    error_types.add(type(item).__name__)
            await asyncio.to_thread(
                state.append_record,
                owner,
                assessment_id,
                "execution_error",
                str(uuid4()),
                {"generation": current["generation"], "error_types": sorted(error_types)},
            )
        raise


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
        apify_token: str | None = None,
        github_token: str | None = None,
        sandboxes=SmolSandbox,
        model_factory=None,
        decision_factory=None,
        callbacks: list | None = None,
        tracing: Tracing | None = None,
    ):
        if service.execution_mode == "live" and (
            not (openrouter_key or plan_credentials) or not exa_key or model_factory
        ):
            raise ValueError(
                "Live coordination requires provider keys and the accounted model factory."
            )
        if service.execution_mode == "live" and not openrouter_key:
            raise ValueError("Live coordination needs OpenRouter for Claude subagents and Jev.")
        if service.execution_mode == "simulation" and (
            model_factory is None or decision_factory is None
        ):
            raise ValueError(
                "Simulation requires explicit non-provider model and decision factories."
            )
        self.service, self.state = service, service.state
        self.checkpoint_dsn, self.application_dsn = checkpoint_dsn, application_dsn
        self.image, self.artifacts = image, artifacts.resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._router_key, self._exa_key, self._apify_token = openrouter_key, exa_key, apify_token
        self._github_token = github_token
        # The sandbox backend class: SmolSandbox locally, ModalSandbox in the cloud. `image` is
        # that backend's image reference (a SmolVM archive, or the Modal base image).
        self.sandboxes = sandboxes
        self._decision_factory = decision_factory
        # ChatGPT plan usage takes precedence when the operator signed in for it.
        self.plan_credentials = plan_credentials
        self._model_factory, self.callbacks = model_factory, callbacks or []
        self.tracing = tracing
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
            "repository_candidates": candidates,
            "private_terms": list(private_terms or ()),
        }
        self.state.append_record(owner, assessment_id, "execution_plan", "main", plan)
        return plan

    def _completed_unit(self, owner, assessment_id, role):
        # A completed worker is reused only while its accepted outputs and inputs are unchanged.
        heads = self.service._heads(self.state.records(owner, assessment_id))
        exports = {
            item["record_key"]: item["payload"]
            for item in self.state.records(owner, assessment_id, kind="worker_export")
        }
        for worker in reversed(self.state.workers(owner, assessment_id)):
            if worker["worker_key"] != role or worker["status"] != "completed":
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
        """Accepted records a worker builds on; a changed input invalidates its saved result.

        The lead starts from the brief alone; the planner builds on the analysis and decision.
        """
        if role == "lead":
            return {}
        return {
            key: record["content_hash"]
            for key, record in heads.items()
            if key.startswith(("analysis:", "decision:"))
        }

    async def _stop_machines(self, owner, assessment_id):
        workers = await asyncio.to_thread(self.state.workers, owner, assessment_id)
        names = {worker["machine_id"] or self.machine_name(worker): worker for worker in workers}
        for name in await asyncio.to_thread(self.sandboxes.running, "condere-"):
            index_machine = re.fullmatch(rf"condere-index-{re.escape(assessment_id)}-[0-9]+", name)
            if name not in names and not index_machine:
                continue
            artifact = (
                names[name]["id"]
                if name in names
                else name.split("-")[1] + "-" + name.rsplit("-", 1)[1]
            )
            sandbox = await asyncio.to_thread(
                self.sandboxes.reconnect, name, self.artifacts / assessment_id / artifact
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
                await asyncio.to_thread(
                    self.state.settle_small_uncertain, owner, assessment_id, SMALL_CHARGE_MICROUSD
                )
            plan = await asyncio.to_thread(
                self._plan, owner, assessment_id, current["brief"], private_terms
            )
            case = await asyncio.to_thread(self.service.begin, owner, assessment_id, resume=resume)
            generation = case["generation"]
            self._tasks[(owner, assessment_id)] = asyncio.current_task()
            try:
                await self._index(owner, assessment_id, generation, plan)
                await asyncio.to_thread(self.service.save_original_input, owner, assessment_id)
                await asyncio.to_thread(self._reuse, owner, assessment_id)
                async with checkpoint_store(
                    self.checkpoint_dsn, application_dsn=self.application_dsn
                ) as checkpointer:
                    await self._run_unit(
                        owner, assessment_id, generation, "lead", plan, checkpointer
                    )
                decided = await asyncio.to_thread(self._decide, owner, assessment_id, generation)
                chosen = decided["decision"]
                if chosen["option"]["value"] != "none" and chosen["verdict"]["value"] != "stop":
                    async with checkpoint_store(
                        self.checkpoint_dsn, application_dsn=self.application_dsn
                    ) as checkpointer:
                        await self._run_unit(
                            owner, assessment_id, generation, "planner", plan, checkpointer
                        )
                await asyncio.to_thread(
                    self.state.settle_small_uncertain, owner, assessment_id, SMALL_CHARGE_MICROUSD
                )
                result = await asyncio.to_thread(
                    self.service.finalize_spinoff, owner, assessment_id, generation
                )
                await asyncio.to_thread(self._trace_funnel, owner, assessment_id, generation)
                return result
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

    def _decide(self, owner, assessment_id, generation) -> dict:
        """Jev decides from the saved analysis; an unchanged analysis reuses its decision."""
        analysis_hash = self.service.analysis_hash(owner, assessment_id)
        saved = self.service.decision(owner, assessment_id)
        if saved is not None and saved["analysis_hash"] == analysis_hash:
            return saved
        context = ProviderContext(self.state, owner, assessment_id, generation, "decision")
        jev = (
            self._decision_factory(context)
            if self._decision_factory
            else JevDecisions(context, self._router_key)
        )
        try:
            decision, brief = decide(
                jev,
                self.service.analysis(owner, assessment_id),
                self.state.get(owner, assessment_id)["brief"]["original_input"],
                assessment_id,
            )
        finally:
            if hasattr(jev, "close"):
                jev.close()
        saved = self.service.record_decision(owner, assessment_id, decision, brief, analysis_hash)[
            "payload"
        ]
        if self.tracing is not None:
            self.tracing.record(
                "condere.decision",
                inputs={"brief": brief, "analysis_hash": analysis_hash},
                outputs=saved["decision"],
                metadata=self._step_metadata(assessment_id, generation, "decision"),
                tags=["condere", "decision"],
                scores={
                    "verdict_confidence": decision.verdict.confidence,
                    "contested_count": len(decision.contested),
                    "code_adjustments": len(decision.adjustments),
                },
            )
        return saved

    def _step_metadata(self, assessment_id, generation, role) -> dict:
        return {
            "assessment_id": assessment_id,
            "generation": generation,
            "role": role,
            "execution_mode": self.service.execution_mode,
        }

    def _trace_funnel(self, owner, assessment_id, generation):
        """Trace how far each research route's sources travel toward the decision."""
        if self.tracing is None:
            return
        funnel = self.service.funnel(owner, assessment_id)
        stages, claims = funnel["stages"], funnel["claims"]
        saved = stages["saved"]["sources"]
        self.tracing.record(
            "condere.funnel",
            inputs={"assessment_id": assessment_id},
            outputs=funnel,
            metadata=self._step_metadata(assessment_id, generation, "funnel"),
            tags=["condere", "funnel"],
            scores={
                "sources_saved": saved,
                "sources_cited": stages["cited"]["sources"],
                "decisive_origin_groups": stages["decisive"]["origin_groups"],
                "citation_yield": round(stages["cited"]["sources"] / saved, 3) if saved else None,
                "candidate_use": round(claims["candidates_used"] / claims["candidates"], 3)
                if claims["candidates"]
                else None,
                "candidates_reused": claims["candidates_reused"],
            },
        )

    def _worker_trace(self, worker, assessment_id, generation, brief, indexes, reused=()):
        if self.tracing is None:
            return contextlib.nullcontext(WorkerTrace())
        assignment = worker["assignment"]
        return self.tracing.worker(
            "condere." + assignment["role"],
            inputs={
                "brief": brief,
                "repositories": [
                    {"repository": item["repository"], "commit": item["commit"]} for item in indexes
                ],
            },
            metadata={
                "assessment_id": assessment_id,
                "worker_id": worker["id"],
                "generation": generation,
                "role": assignment["role"],
                "skills": assignment["skills"],
                "execution_mode": self.service.execution_mode,
                "reused_code_claims": sum(item["claims"] for item in reused),
                "reused_from": sorted({item["reused_from"] for item in reused}),
            },
            tags=["condere", assignment["role"]],
        )

    def _reuse(self, owner, assessment_id):
        """Reuse verified code claims for a pinned commit; failure only means no reuse."""
        try:
            self.service.reuse_code_claims(owner, assessment_id)
        except (Conflict, EvidenceError, NotFound, ValueError) as error:
            logging.getLogger(__name__).warning(
                "Code-analysis reuse was skipped (%s).", type(error).__name__
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
                self.sandboxes.create,
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

    async def _run_unit(self, owner, assessment_id, generation, role, plan, checkpointer):
        previous = await asyncio.to_thread(self._completed_unit, owner, assessment_id, role)
        if previous is not None:
            return previous
        worker = await asyncio.to_thread(
            self.service.assign_worker, owner, assessment_id, generation, role
        )
        artifact_dir = self.artifacts / assessment_id / worker["id"]
        inputs = self._input_hashes(
            self.service._heads(await asyncio.to_thread(self.state.records, owner, assessment_id)),
            role,
        )
        creation = asyncio.create_task(
            asyncio.to_thread(
                self.sandboxes.create, self.machine_name(worker), self.image, artifact_dir
            )
        )
        try:
            sandbox = await asyncio.shield(creation)
        except asyncio.CancelledError:
            sandbox = await creation
            await asyncio.to_thread(sandbox.close)
            raise
        succeeded, research, social, community, model, subagents = False, None, None, None, None, []
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
            for record in indexes:
                await asyncio.to_thread(
                    install_index, self.service.evidence, owner, assessment_id, record, sandbox
                )
            brief = (await asyncio.to_thread(self.state.get, owner, assessment_id))["brief"]
            context = ProviderContext(self.state, owner, assessment_id, generation, role)
            model = (
                self._model_factory(context)
                if self._model_factory
                else plan_model(context, self.plan_credentials)
                if self.plan_credentials is not None
                else accounted_model(context, self._router_key)
            )
            if self.service.execution_mode == "live":
                research = ExaResearch(
                    context,
                    self.service.evidence,
                    self._exa_key,
                    private_terms=tuple(plan["private_terms"]),
                )
                if role == "lead" and self._apify_token:
                    social = ApifyResearch(
                        context,
                        self.service.evidence,
                        self._apify_token,
                        private_terms=tuple(plan["private_terms"]),
                    )
                if role == "lead":
                    community = CommunityResearch(
                        context,
                        self.service.evidence,
                        github_token=self._github_token,
                        private_terms=tuple(plan["private_terms"]),
                    )
            if role == "lead":
                for subagent, spec in (("code-analyst", CLAUDE_OPUS), ("extractor", CLAUDE_HAIKU)):
                    ledger = ProviderContext(self.state, owner, assessment_id, generation, subagent)
                    subagents.append(
                        self._model_factory(ledger)
                        if self._model_factory
                        # Subagents run on ChatGPT plan usage when the owner signed in (code
                        # analysis on GPT-6 Astra, extraction on GPT-6.1 Terra); Claude on
                        # OpenRouter remains the paid alternative.
                        else plan_model(
                            ledger,
                            self.plan_credentials,
                            model=PLAN_CODE_MODEL
                            if subagent == "code-analyst"
                            else PLAN_EXTRACT_MODEL,
                        )
                        if self.plan_credentials is not None
                        else accounted_model(ledger, self._router_key, spec=spec)
                    )
            worker_trace = WorkerTrace()
            try:
                reused = (
                    await asyncio.to_thread(self.service.reused_code_analysis, owner, assessment_id)
                    if role == "lead"
                    else []
                )
                with self._worker_trace(
                    worker, assessment_id, generation, brief, indexes, reused
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
                        callbacks=self.callbacks,
                        social=social,
                        community=community,
                        code_model=subagents[0] if subagents else None,
                        extract_model=subagents[1] if subagents else None,
                    )
                    accepted = self.service._heads(
                        await asyncio.to_thread(self.state.records, owner, assessment_id)
                    )
                    answer = {
                        key: accepted[key]["payload"]
                        for key in completed["completion"]["accepted_hashes"]
                    }
                    worker_trace.outputs(answer)
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
            for adapter in (research, social, community):
                if adapter is not None:
                    adapter.close()
            for item in (model, *subagents):
                if isinstance(item, (AccountedChatOpenAI, PlanChatOpenAI)):
                    await asyncio.to_thread(item.root_client.close)
                    await item.root_async_client.close()
            await asyncio.to_thread(sandbox.close, delete=succeeded)
