"""Execute one assigned Deep Agents worker with durable state and an isolated machine."""

import asyncio
import json
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from condere.runtime.agent import build_agent, build_lead_agent
from condere.runtime.chatgpt_plan import PlanChatOpenAI
from condere.runtime.providers import AccountedChatOpenAI, ApifyResearch, ExaResearch
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.service import AssessmentService, output_keys
from condere.runtime.state import Conflict
from condere.runtime.tools import worker_tools


async def run_worker(
    service: AssessmentService,
    *,
    owner: str,
    assessment_id: str,
    generation: int,
    role: str,
    sandbox: SmolSandbox,
    model: BaseChatModel,
    checkpointer,
    research: ExaResearch | None,
    repository_cache: Path,
    allowed_repositories: set[str],
    callbacks: list | None = None,
    social: ApifyResearch | None = None,
    code_model: BaseChatModel | None = None,
    extract_model: BaseChatModel | None = None,
) -> dict:
    """Complete one assignment; the coordinator owns scheduling, cancellation, and VM lifecycle."""
    if checkpointer is None:
        raise ValueError("An assessment worker requires a durable checkpointer.")
    if service.execution_mode == "live":
        if not isinstance(model, (AccountedChatOpenAI, PlanChatOpenAI)):
            raise ValueError("Live workers require the accounted model adapter.")
        context = model._context
        if context.state is not service.state or (
            context.owner,
            context.assessment_id,
            context.generation,
            context.worker_role,
        ) != (owner, assessment_id, generation, role):
            raise Conflict("The model must use the assigned worker's spending ledger.")
        if research is None:
            raise ValueError("A live research worker requires its accounted research adapter.")
        if role == "lead":
            for subagent in (code_model, extract_model):
                if not isinstance(subagent, (AccountedChatOpenAI, PlanChatOpenAI)):
                    raise ValueError("Live subagents require accounted model adapters.")
                ledger = subagent._context
                if ledger.state is not service.state or (
                    ledger.owner,
                    ledger.assessment_id,
                    ledger.generation,
                ) != (owner, assessment_id, generation):
                    raise Conflict("Subagent models must use this assessment's spending ledger.")
    if role == "lead" and (code_model is None or extract_model is None):
        raise ValueError("The lead requires code-analyst and extractor models.")
    assigned = await asyncio.to_thread(
        service.assign_worker, owner, assessment_id, generation, role
    )
    if assigned["status"] == "completed":
        return assigned
    assigned = await asyncio.to_thread(
        service.state.attach_machine, owner, assessment_id, generation, assigned["id"], sandbox.id
    )
    try:
        tools = await asyncio.to_thread(
            worker_tools,
            service,
            owner=owner,
            assessment_id=assessment_id,
            generation=generation,
            worker_id=assigned["id"],
            research=research,
            sandbox=sandbox,
            repository_cache=repository_cache,
            allowed_repositories=allowed_repositories,
            social=social,
        )
        assignment = json.dumps(assigned["assignment"], sort_keys=True)
        if role == "lead":
            assignment += (
                "\nStart with read_assessment for the brief and the exact Analysis schema. "
                "You may use the sandbox tools (execute, ls, read_file, grep, glob) inside "
                "/workspace, and the saved repository index under repository_indexes. Submit "
                "the analysis with submit_analysis; a final message alone does not complete "
                "this assignment. Repeated rejected submissions stop the work, so read each "
                "error and fix its cause. Do not request internal-stage human approval."
            )
            agent = await asyncio.to_thread(
                build_lead_agent,
                model,
                sandbox,
                assignment=assignment,
                skills=assigned["assignment"]["skills"],
                tools=tools,
                code_model=code_model,
                extract_model=extract_model,
                checkpointer=checkpointer,
            )
        else:
            assignment += (
                "\nStart with read_assessment for the brief, the accepted analysis and the "
                "decision, and the exact BuildPlan schema. You may use the sandbox tools "
                "inside /workspace and the saved repository index. Submit with "
                "submit_build_plan; a final message alone does not complete this assignment."
            )
            agent = await asyncio.to_thread(
                build_agent,
                model,
                sandbox,
                assignment=assignment,
                skills=assigned["assignment"]["skills"],
                tools=tools,
                checkpointer=checkpointer,
            )
        config = {
            "configurable": {"thread_id": assigned["thread_id"]},
            "metadata": {
                "assessment_id": assessment_id,
                "execution_mode": service.execution_mode,
                "worker_id": assigned["id"],
                "generation": generation,
                "role": role,
            },
            "callbacks": callbacks or [],
        }
        saved = await agent.aget_state(config)
        if not saved.values or saved.next:
            await agent.ainvoke(
                None
                if saved.values
                else {
                    "messages": [
                        {"role": "user", "content": "Complete your trusted Condere assignment."}
                    ],
                    # The planner's skills load up front (Deep Agents skill pinning). The
                    # lead's prompt names the skill for each stage, so it loads them on demand.
                    "pinned_skills": [] if role == "lead" else assigned["assignment"]["skills"],
                },
                config=config,
            )
        await asyncio.to_thread(service._worker, owner, assessment_id, generation, assigned["id"])
        heads = service._heads(await asyncio.to_thread(service.state.records, owner, assessment_id))
        keys = output_keys(assigned["assignment"])
        if any(key not in heads for key in keys):
            raise Conflict("The worker ended without submitting every assigned result.")
        artifact = {
            key: {"content_hash": heads[key]["content_hash"], "payload": heads[key]["payload"]}
            for key in keys
        }
        exported = await asyncio.to_thread(
            sandbox.upload_files,
            [
                (
                    assigned["assignment"]["output_path"],
                    json.dumps(artifact, sort_keys=True).encode(),
                )
            ],
        )
        if any(item.error for item in exported):
            raise RuntimeError(
                "Accepted findings are saved, but their worker artifact export failed."
            )
        return await asyncio.to_thread(
            service.finish_worker, owner, assessment_id, generation, assigned["id"]
        )
    except BaseException:
        try:
            await asyncio.to_thread(service.state.stop, owner, assessment_id, generation=generation)
        except Conflict:
            pass  # A stale worker must not interrupt a newer continuation.
        raise
