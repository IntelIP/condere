"""Assemble a private Deep Agents worker with Condere's preserved methods."""

import hashlib
from pathlib import Path

from deepagents import create_deep_agent
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends import CompositeBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelResponse,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage

from condere.runtime.operations import ProviderRefused, ProviderUncertain
from condere.runtime.providers import CLAUDE_OPUS, WORKING_CONTEXT_TOKENS
from condere.runtime.sandbox import CodeSandbox
from condere.runtime.spinoff import ClaimBatch

METHODS_ROOT = Path(__file__).resolve().parents[3] / "agent"
# Live runs on 2026-10-28 resent every earlier tool result on each call (~20 calls,
# 7k -> 40k input tokens per worker) and the plan route gave little prompt caching.
# Clear older tool outputs past this size; saved sources stay retrievable by ID.
# With the 200k working context (no early summaries), a 24k trigger cleared tool output on
# every turn and agents re-read the same sources: on 2026-10-30 one extractor called
# read_source 78 times and the code analyst search_saved_sources 41 times.
CONTEXT_EDIT_TRIGGER_TOKENS = 80_000
PROTECTED_TOOLS = ("read_assessment", "submit_analysis")
# The instructions each model reads; they and the skills determine the method version.
PROMPTS = ("lead.md", "planner.md", "subagents/code-analyst.md", "subagents/extractor.md")


def method_files(root: Path = METHODS_ROOT, skills: list[str] | None = None) -> dict[str, bytes]:
    """All methods, or only the named skills a worker is assigned."""
    available = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    if skills is not None and not set(skills).issubset(available):
        raise ValueError("A worker was assigned an unknown skill.")
    files = {
        "/workspace/" + str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "skills").rglob("*"))
        if path.is_file()
        and path.suffix in {".md", ".py"}
        and (skills is None or path.relative_to(root / "skills").parts[0] in skills)
    }
    files["/workspace/context/business.md"] = (root / "context/business.md").read_bytes()
    return files


def method_version(root: Path = METHODS_ROOT) -> str:
    digest = hashlib.sha256()
    for prompt in PROMPTS:
        digest.update(prompt.encode())
        digest.update(hashlib.sha256((root / prompt).read_bytes()).digest())
    for path, content in method_files(root).items():
        digest.update(path.encode())
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


# What determines the code analyst's claims, and so whether another assessment's claims for
# the same repository commit can be reused: its prompt, the two skills its prompt directs it
# to (index and Graphify navigation), and its model. Lead, extractor and planner edits, and
# the other skills, do not change it.
CODE_ANALYST_SKILLS = ["repository-index", "code-exploration"]


def code_method_version(root: Path = METHODS_ROOT, model_id: str = CLAUDE_OPUS.id) -> str:
    digest = hashlib.sha256(model_id.encode())
    digest.update(hashlib.sha256((root / "subagents/code-analyst.md").read_bytes()).digest())
    for path, content in method_files(root, CODE_ANALYST_SKILLS).items():
        if path.startswith("/workspace/skills/"):
            digest.update(path.encode())
            digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def build_agent(
    model: BaseChatModel,
    sandbox: CodeSandbox,
    *,
    assignment: str,
    skills: list[str] | None = None,
    tools=(),
    checkpointer=None,
    methods_root: Path = METHODS_ROOT,
):
    """The build planner, in its own VM; nested shared-VM delegation is disabled."""
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
    instructions = (methods_root / "planner.md").read_text()
    instructions += (
        "\n\nYour trusted assignment:\n"
        + assignment
        + "\n\nApproved business context is at /workspace/context/business.md. "
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
    "submit_claims",
    "read_assessment",
    "list_sources",
    "search_saved_sources",
    "read_source",
    "inspect_repository",
}
EXTRACTOR_TOOLS = {
    "submit_claims",
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
    "hacker_news_search",
    "github_issues_search",
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


PROVIDER_FAILURES = (ProviderRefused, ProviderUncertain)


class ProviderFailures(AgentMiddleware):
    """A failed provider call becomes a message the agent can act on, not the end of the run.

    Tools answer with the failure, so the agent can use another route. A subagent whose own
    model call fails ends its task with a note; the claims it saved stay available to the
    lead. The lead's model failures still propagate and interrupt the assessment. On
    2026-10-30 one refused Apify start and one Haiku timeout each ended a 30-minute run.
    """

    def __init__(self, *, end_task_on_model_failure: bool):
        super().__init__()
        self.end_task = end_task_on_model_failure

    @staticmethod
    def _tool_failure(request, error) -> ToolMessage:
        return ToolMessage(
            content=f"{error} Continue with another route or without this result.",
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )

    @staticmethod
    def _task_failure(error) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    content=f"This task stopped because its model call failed: {error} Claims "
                    "already saved with submit_claims remain available to the lead."
                )
            ]
        )

    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        except PROVIDER_FAILURES as error:
            return self._tool_failure(request, error)

    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except PROVIDER_FAILURES as error:
            return self._tool_failure(request, error)

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except PROVIDER_FAILURES as error:
            if not self.end_task:
                raise
            return self._task_failure(error)

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except PROVIDER_FAILURES as error:
            if not self.end_task:
                raise
            return self._task_failure(error)


# The ChatGPT plan route ended a GPT-6 Astra code-analyst request with `max_messages` at 76
# input items (37 tool calls) on 2026-10-30, at ~50k tokens. Context editing shortens old
# outputs but keeps every item, so subagents also summarise by message count first.
SUBAGENT_MESSAGE_LIMIT = 56


def _identity(model: BaseChatModel) -> str:
    identifier, provider = get_model_identifier(model), get_model_provider(model)
    profile_key = f"{provider}:{identifier}" if provider and identifier else identifier
    if not profile_key or ":" not in profile_key:
        raise ValueError("The worker model needs a resolvable identity for its isolation profile.")
    return profile_key


def _isolate(model: BaseChatModel, *, own_summary: bool = False) -> None:
    """Disable the automatic general-purpose subagent for this model identity.

    With `own_summary`, the default token-based summarisation is replaced by the subagent's
    message-capped one (`_subagent_summary`).
    """
    register_harness_profile(
        _identity(model),
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_middleware=frozenset({"SummarizationMiddleware"} if own_summary else ()),
        ),
    )


def _subagent_summary(model: BaseChatModel, backend) -> SummarizationMiddleware:
    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=[
            ("messages", SUBAGENT_MESSAGE_LIMIT),
            ("tokens", int(WORKING_CONTEXT_TOKENS * 0.85)),
        ],
        keep=("messages", 16),
        trim_tokens_to_summarize=None,
    )


def build_lead_agent(
    model: BaseChatModel,
    sandbox: CodeSandbox,
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
    _isolate(model)
    # A subagent sharing the lead's model identity keeps the lead's profile and summarisation.
    own_summary = {
        name: _identity(item) != _identity(model)
        for name, item in (("code-analyst", code_model), ("extractor", extract_model))
    }
    for item, name in ((code_model, "code-analyst"), (extract_model, "extractor")):
        _isolate(item, own_summary=own_summary[name])
    backend = CompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace")

    def summary(item, name):
        return [_subagent_summary(item, backend)] if own_summary[name] else []

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
            "middleware": [
                ProviderFailures(end_task_on_model_failure=True),
                *summary(code_model, "code-analyst"),
                _context_editing(),
            ],
        },
        {
            "name": "extractor",
            "description": (
                "Researches one source route (Reddit and Hacker News, X, news and pricing, or "
                "YouTube) or reads given sources, and returns attributed claims with exact "
                "quotes and publication dates. Give it the buyer questions, the route or source "
                "IDs, and the recency window. Several can run in parallel."
            ),
            "system_prompt": (methods_root / "subagents/extractor.md").read_text(),
            "model": extract_model,
            "tools": chosen(EXTRACTOR_TOOLS),
            "skills": ["/workspace/skills/"],
            "response_format": ToolStrategy(ClaimBatch),
            "middleware": [
                ProviderFailures(end_task_on_model_failure=True),
                *summary(extract_model, "extractor"),
                _context_editing(),
            ],
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
        backend=backend,
        skills=["/workspace/skills/"],
        subagents=subagents,
        middleware=[ProviderFailures(end_task_on_model_failure=False), _context_editing()],
        checkpointer=checkpointer,
        name="condere-lead",
    )
