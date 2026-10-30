"""Assemble a private Deep Agents worker with Condere's preserved methods."""

import hashlib
import json
from pathlib import Path

from deepagents import create_deep_agent
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends import CompositeBackend
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel

from condere.runtime.sandbox import SmolSandbox
from condere.runtime.spinoff import ClaimBatch

METHODS_ROOT = Path(__file__).resolve().parents[3] / "agent"
# Live runs on 2026-10-28 resent every earlier tool result on each call (~20 calls,
# 7k -> 40k input tokens per worker) and the plan route gave little prompt caching.
# Clear older tool outputs past this size; saved sources stay retrievable by ID.
CONTEXT_EDIT_TRIGGER_TOKENS = 24_000
PROTECTED_TOOLS = (
    "read_assessment",
    "submit_question",
    "submit_screen",
    "submit_review",
    "submit_analysis",
)
CORE_SKILLS = {
    "opportunity-framing",
    "repository-index",
    "code-exploration",
    "evidence-reconciliation",
    "dossier-and-handoff",
    "visual-reporting",
}


def method_files(root: Path = METHODS_ROOT, skills: list[str] | None = None) -> dict[str, bytes]:
    """All methods, or only the named skills a worker is assigned."""
    available = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    referenced = {
        skill
        for question in json.loads((root / "references/questions.json").read_text()).values()
        for skill in question["skills"]
    }
    if not (referenced | CORE_SKILLS).issubset(available):
        raise ValueError("Condere requires every core and question-assigned research skill.")
    if skills is not None and not set(skills).issubset(available):
        raise ValueError("A worker was assigned an unknown skill.")
    files = {
        "/workspace/" + str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "skills").rglob("*"))
        if path.is_file()
        and path.suffix in {".md", ".py"}
        and (skills is None or path.relative_to(root / "skills").parts[0] in skills)
    }
    files["/workspace/methods/questions.json"] = (root / "references/questions.json").read_bytes()
    files["/workspace/context/business.md"] = (root / "context/business.md").read_bytes()
    return files


def method_version(root: Path = METHODS_ROOT) -> str:
    digest = hashlib.sha256((root / "instructions.md").read_bytes())
    for path, content in method_files(root).items():
        digest.update(path.encode())
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def build_agent(
    model: BaseChatModel,
    sandbox: SmolSandbox,
    *,
    assignment: str,
    skills: list[str] | None = None,
    tools=(),
    checkpointer=None,
    methods_root: Path = METHODS_ROOT,
):
    """The application creates separate workers/VMs; nested shared-VM delegation is disabled."""
    identifier, provider = get_model_identifier(model), get_model_provider(model)
    profile_key = f"{provider}:{identifier}" if provider and identifier else identifier
    if not profile_key or ":" not in profile_key:
        raise ValueError("The worker model needs a resolvable identity for its isolation profile.")
    # Deep Agents 0.7.29 resolves profiles by model identity, not by worker identity.
    register_harness_profile(
        profile_key,
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )
    transferred = sandbox.upload_files(list(method_files(methods_root, skills).items()))
    if any(item.error for item in transferred):
        raise RuntimeError("Could not load all required Condere methods into the assigned sandbox.")
    instructions = (methods_root / "instructions.md").read_text()
    instructions += (
        "\n\nYour trusted assignment:\n"
        + assignment
        + "\n\nQuestion methods are at /workspace/methods/questions.json. "
        "Approved business context is at /workspace/context/business.md. "
        "Method copies are reference material; editing them cannot change the application contract."
    )
    return create_deep_agent(
        model=model,
        tools=list(tools),
        system_prompt=instructions,
        backend=CompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace"),
        skills=["/workspace/skills/"],
        subagents=[],
        middleware=[
            ContextEditingMiddleware(
                edits=[
                    ClearToolUsesEdit(
                        trigger=CONTEXT_EDIT_TRIGGER_TOKENS,
                        keep=4,
                        exclude_tools=PROTECTED_TOOLS,
                        placeholder=(
                            "[Older tool output cleared to save context. Re-read it with "
                            "read_source or search_saved_sources if you still need it.]"
                        ),
                    )
                ]
            )
        ],
        checkpointer=checkpointer,
        name="condere-worker",
    )


CODE_ANALYST_TOOLS = {
    "read_assessment",
    "list_sources",
    "search_saved_sources",
    "read_source",
    "inspect_repository",
}
EXTRACTOR_TOOLS = {
    "read_assessment",
    "list_sources",
    "search_saved_sources",
    "read_source",
    "search_public",
    "fetch_public_source",
    "fetch_public_url",
    "x_posts",
    "x_conversation",
    "x_search",
    "reddit_research",
    "youtube_transcripts",
}


def _context_editing():
    return ContextEditingMiddleware(
        edits=[
            ClearToolUsesEdit(
                trigger=CONTEXT_EDIT_TRIGGER_TOKENS,
                keep=4,
                exclude_tools=PROTECTED_TOOLS,
                placeholder=(
                    "[Older tool output cleared to save context. Re-read it with "
                    "read_source or search_saved_sources if you still need it.]"
                ),
            )
        ]
    )


def _isolate(model: BaseChatModel) -> None:
    """Disable the automatic general-purpose subagent for this model identity."""
    identifier, provider = get_model_identifier(model), get_model_provider(model)
    profile_key = f"{provider}:{identifier}" if provider and identifier else identifier
    if not profile_key or ":" not in profile_key:
        raise ValueError("The worker model needs a resolvable identity for its isolation profile.")
    register_harness_profile(
        profile_key,
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )


def build_lead_agent(
    model: BaseChatModel,
    sandbox: SmolSandbox,
    *,
    assignment: str,
    skills: list[str],
    tools=(),
    code_model: BaseChatModel,
    extract_model: BaseChatModel,
    checkpointer=None,
    methods_root: Path = METHODS_ROOT,
):
    """The lead plans and writes the analysis; two declared subagents do bounded work.

    Subagents share the lead's sandbox, so their tools are narrowed to reading and
    research; only the lead can submit. Each model is an accounted adapter, so every
    subagent call is recorded in the spending ledger.
    """
    for item in (model, code_model, extract_model):
        _isolate(item)
    files = method_files(methods_root, skills)
    for name in ("code-analyst", "extractor"):
        files[f"/workspace/subagents/{name}.md"] = (
            methods_root / "subagents" / f"{name}.md"
        ).read_bytes()
    transferred = sandbox.upload_files(list(files.items()))
    if any(item.error for item in transferred):
        raise RuntimeError("Could not load all required Condere methods into the assigned sandbox.")
    tools = list(tools)

    def chosen(names):
        return [item for item in tools if item.name in names]

    subagents = [
        {
            "name": "code-analyst",
            "description": (
                "Reads and explains source code in the sandbox using the saved repository "
                "index, Graphify and grep. Give it the repository, the question and any "
                "suggested paths; it returns claims with exact quotes and components."
            ),
            "system_prompt": (methods_root / "subagents/code-analyst.md").read_text(),
            "model": code_model,
            "tools": chosen(CODE_ANALYST_TOOLS),
            "skills": ["/workspace/skills/"],
            "response_format": ToolStrategy(ClaimBatch),
            "middleware": [_context_editing()],
        },
        {
            "name": "extractor",
            "description": (
                "Reads saved sources, or runs a named search, and returns attributed claims "
                "with exact quotes and publication dates. Give it the question and the "
                "source IDs or search to run."
            ),
            "system_prompt": (methods_root / "subagents/extractor.md").read_text(),
            "model": extract_model,
            "tools": chosen(EXTRACTOR_TOOLS),
            "response_format": ToolStrategy(ClaimBatch),
            "middleware": [_context_editing()],
        },
    ]
    instructions = (methods_root / "lead.md").read_text() + (
        "\n\nYour trusted assignment:\n"
        + assignment
        + "\n\nApproved business context is at /workspace/context/business.md. "
        "Method copies are reference material; editing them cannot change the application contract."
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=instructions,
        backend=CompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace"),
        skills=["/workspace/skills/"],
        subagents=subagents,
        middleware=[_context_editing()],
        checkpointer=checkpointer,
        name="condere-lead",
    )
