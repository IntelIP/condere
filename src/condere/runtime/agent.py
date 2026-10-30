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
from langchain_core.language_models import BaseChatModel

from condere.runtime.sandbox import SmolSandbox

METHODS_ROOT = Path(__file__).resolve().parents[3] / "agent"
# Live runs on 2026-10-28 resent every earlier tool result on each call (~20 calls,
# 7k -> 40k input tokens per worker) and the plan route gave little prompt caching.
# Clear older tool outputs past this size; saved sources stay retrievable by ID.
CONTEXT_EDIT_TRIGGER_TOKENS = 24_000
PROTECTED_TOOLS = ("read_assessment", "submit_question", "submit_screen", "submit_review")
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
