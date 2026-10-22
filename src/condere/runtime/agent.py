"""Assemble a private Deep Agents worker with Condere's preserved methods."""

import hashlib
from pathlib import Path

from deepagents import create_deep_agent
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel

from condere.runtime.sandbox import SmolSandbox

METHODS_ROOT = Path(__file__).resolve().parents[3] / "agent"


def method_files(root: Path = METHODS_ROOT) -> dict[str, bytes]:
    files = {
        "/workspace/" + str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "skills").glob("*/SKILL.md"))
    }
    if len(files) != 6:
        raise ValueError("Condere requires all six research skills.")
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
    transferred = sandbox.upload_files(list(method_files(methods_root).items()))
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
        backend=sandbox,
        skills=["/workspace/skills/"],
        subagents=[],
        checkpointer=checkpointer,
        name="condere-worker",
    )
