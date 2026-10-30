"""Real local-engine checks; enable explicitly with CONDERE_TEST_SANDBOX=1."""

import asyncio
import hashlib
import json
import os
import shlex
import uuid
from pathlib import Path

import pytest
from deepagents import graph
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from condere.runtime.agent import build_agent
from condere.runtime.repositories import acquire_repository, stage_repository
from condere.runtime.sandbox import SmolSandbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CONDERE_TEST_SANDBOX") != "1", reason="Local VM test not enabled"
    ),
]


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory):
    image = os.environ["CONDERE_SMOL_IMAGE"]
    directory = tmp_path_factory.mktemp("smolvm")
    created = []
    environment = pytest.MonkeyPatch()
    for key in ("OPENROUTER_API_KEY", "EXA_API_KEY", "DOPPLER_TOKEN", "SMOL_CLOUD_TOKEN"):
        environment.setenv(key, "synthetic-inheritance-check-not-a-credential")
    try:
        for suffix in ("a", "b"):
            name = f"condere-test-{uuid.uuid4().hex[:12]}-{suffix}"
            created.append(SmolSandbox.create(name, image, directory / suffix))
        yield created
    finally:
        for sandbox in created:
            sandbox.close(delete=True)
        environment.undo()


def test_isolation_and_file_export(sandboxes, tmp_path):
    first, second = sandboxes
    assert first.id != second.id
    payload = b"a finding exported from an isolated VM\n"
    assert first.upload_files([("/workspace/finding.txt", payload)])[0].error is None
    assert first.download_files(["/workspace/finding.txt"])[0].content == payload
    assert second.download_files(["/workspace/finding.txt"])[0].error == "file_not_found"
    host_file = tmp_path / "host-only.txt"
    host_file.write_text("host-only-marker")
    script = f"""
import json, os, pathlib, socket
sock = socket.socket()
sock.settimeout(2)
print(json.dumps({{
    'network_denied': sock.connect_ex(('1.1.1.1', 443)) != 0,
    'host_file_absent': not pathlib.Path({str(host_file)!r}).exists(),
    'socket_absent': not pathlib.Path('/var/run/docker.sock').exists(),
    'secrets_absent': not any(k in os.environ for k in
        ('OPENROUTER_API_KEY', 'EXA_API_KEY', 'DOPPLER_TOKEN', 'SMOL_CLOUD_TOKEN')),
}}))
"""
    result = first.execute("python -c " + shlex.quote(script))
    assert result.exit_code == 0
    assert all(json.loads(result.output).values())
    assert first.upload_files([("/workspace/../../host.txt", b"bad")])[0].error == "invalid_path"
    exported = tmp_path / "exported.txt"
    exported.write_bytes(first.download_files(["/workspace/finding.txt"])[0].content)
    assert exported.read_bytes() == payload


def test_deepagents_filesystem_contract_and_failed_command(sandboxes):
    sandbox = sandboxes[0]
    assert sandbox.write("/workspace/tool.txt", "original line\n").error is None
    assert sandbox.edit("/workspace/tool.txt", "original", "revised").error is None
    read = sandbox.read("/workspace/tool.txt")
    assert read.error is None
    assert "revised line" in read.file_data["content"]
    large_text = "\n".join(f"line {index:04d}: " + "x" * 70 for index in range(500))
    assert sandbox.upload_files([("/workspace/large.txt", large_text.encode())])[0].error is None
    large_read = sandbox.read("/workspace/large.txt", limit=500)
    assert large_read.error is None
    assert large_read.file_data["content"] == large_text
    listing = sandbox.ls("/workspace")
    assert listing.error is None
    assert any(item["path"].endswith("tool.txt") for item in listing.entries)
    result = sandbox.execute("printf 'deliberate failure' >&2; exit 17")
    assert result.exit_code == 17
    assert "deliberate failure" in result.output
    assert Path(result.artifact_path).read_text() == result.output


def test_output_is_bounded_and_retained(sandboxes):
    sandbox = sandboxes[0]
    previous_limit = sandbox.output_limit
    sandbox.output_limit = 1024
    try:
        result = sandbox.execute("python -c 'print(\"x\" * 100000)' ")
        assert result.truncated
        assert result.exit_code == 125
        assert len(Path(result.artifact_path).read_bytes()) <= 1024
    finally:
        sandbox.output_limit = previous_limit


async def test_async_execution_and_explicit_cancellation(sandboxes):
    sandbox = sandboxes[0]
    pending = asyncio.create_task(sandbox.aexecute("sleep 60; echo unexpected", timeout=70))
    for _ in range(250):
        if sandbox._stream is not None:
            break
        await asyncio.sleep(0.02)
    assert sandbox._stream is not None, "VM execution must not block the async event loop"
    sandbox.cancel()
    result = await asyncio.wait_for(pending, timeout=10)
    assert result.exit_code != 0
    assert "unexpected" not in result.output


def test_reconnect_and_deletion_preserve_export(sandboxes, tmp_path):
    sandbox = sandboxes[1]
    assert sandbox.upload_files([("/workspace/saved.txt", b"survives")])[0].error is None
    sandbox.close()
    resumed = SmolSandbox.reconnect(sandbox.id, sandbox.artifact_dir)
    sandboxes[1] = resumed
    data = resumed.download_files(["/workspace/saved.txt"])[0].content
    assert data == b"survives"
    exported = tmp_path / "retained.txt"
    exported.write_bytes(data)
    resumed.close(delete=True)
    sandboxes.pop()
    assert exported.read_bytes() == b"survives"


class RehearsalModel(FakeMessagesListChatModel):
    """A deterministic test model; it never calls a provider or evaluates research quality."""

    model_name: str = "condere:fixture"
    tool_names: list[str] = Field(default_factory=list)
    seen_system: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.tool_names = [tool.name if hasattr(tool, "name") else tool["name"] for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_system.extend(str(item.content) for item in messages if item.type == "system")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_deep_agent_uses_real_sandbox_and_discovers_all_skills(sandboxes):
    model = RehearsalModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "fixture-execution",
                        "name": "execute",
                        "args": {
                            "command": "printf 'actual VM output' > /workspace/agent-result.txt"
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "fixture-read",
                        "name": "read_file",
                        "args": {"file_path": "/workspace/agent-result.txt"},
                    }
                ],
            ),
            AIMessage(content="Deterministic infrastructure rehearsal complete; no real research."),
        ]
    )
    agent = build_agent(
        model,
        sandboxes[0],
        assignment="Test only: execute a harmless file roundtrip. No research or provider calls.",
    )
    state = await agent.ainvoke({"messages": [{"role": "user", "content": "Run the rehearsal."}]})
    results = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert len(results) == 2
    assert all(message.status == "success" for message in results)
    assert "actual VM output" in str(results[-1].content)
    assert "execute" in model.tool_names and "task" not in model.tool_names
    prompt = "\n".join(model.seen_system)
    for skill in (
        "asset-understanding",
        "rights-and-ownership",
        "open-core-boundary",
        "buyer-evidence",
        "economics",
        "next-test",
        "build-plan",
        "search-routing",
        "repository-index",
        "code-exploration",
        "evidence-reconciliation",
    ):
        assert skill in prompt


@pytest.mark.asyncio
async def test_compaction_preserves_evicted_messages_in_native_workspace(sandboxes, monkeypatch):
    summarizer = RehearsalModel(responses=[AIMessage(content="Saved earlier fixture context.")])
    monkeypatch.setattr(
        graph,
        "create_summarization_middleware",
        lambda model, backend: SummarizationMiddleware(
            model=summarizer, backend=backend, trigger=("messages", 4), keep=("messages", 2)
        ),
    )
    model = RehearsalModel(responses=[AIMessage(content="Fixture completed after compaction.")])
    agent = build_agent(model, sandboxes[0], assignment="Preserve this fictional conversation.")
    marker = "evicted-fixture-" + uuid.uuid4().hex
    messages = [HumanMessage(content=marker), AIMessage(content="Acknowledged fixture.")]
    for index in range(3):
        messages.extend(
            [HumanMessage(content=f"Fixture context {index}"), AIMessage(content="Recorded.")]
        )
    messages.append(HumanMessage(content="Finish this fixture."))
    result = await agent.ainvoke({"messages": messages})
    assert result["messages"][-1].content == "Fixture completed after compaction."
    command = (
        "import json,pathlib; "
        "print(json.dumps([str(p) for p in "
        "pathlib.Path('/workspace/conversation_history').glob('*.md')]))"
    )
    listing = sandboxes[0].execute("python -c " + shlex.quote(command))
    assert listing.exit_code == 0
    paths = json.loads(listing.output)
    assert paths, "Compaction must save evicted messages inside the writable sandbox workspace."
    files = sandboxes[0].download_files(paths)
    assert all(file.error is None for file in files)
    assert any(marker.encode() in file.content for file in files)
    assert "execute" in model.tool_names and "task" not in model.tool_names


@pytest.mark.skipif(
    os.getenv("CONDERE_TEST_REPOSITORY") != "1", reason="Public GitHub acquisition not enabled"
)
def test_admitted_repository_exact_commit_survives_vm_deletion(sandboxes):
    repository = "https://github.com/asg017/sqlite-vec"
    destination = Path(".artifacts/infrastructure/repositories")
    snapshot = acquire_repository(
        repository, allowed_repositories={repository}, destination=destination
    )
    sandbox = sandboxes[0]
    staged = stage_repository(snapshot, sandbox)
    original = next(entry for entry in snapshot.entries if entry["path"] == "README.md")
    script = "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())"
    result = sandbox.execute(
        " ".join(
            shlex.quote(value) for value in ["python", "-c", script, staged["path"] + "/README.md"]
        )
    )
    assert result.exit_code == 0
    assert result.output.strip() == original["sha256"]
    # An agent may inspect the same snapshot twice after modifying its first working copy.
    sandbox.machine.write_file(staged["path"] + "/README.md", b"Changed first working copy")
    repeated = stage_repository(snapshot, sandbox)
    assert repeated["path"] != staged["path"]
    reread = sandbox.download_files([repeated["path"] + "/README.md"])[0]
    assert reread.error is None
    assert hashlib.sha256(reread.content).hexdigest() == original["sha256"]
    sandbox.close(delete=True)
    sandboxes.pop(0)
    assert snapshot.archive.exists()
    record = {
        **staged,
        "readme_sha256": original["sha256"],
        "archive_survives_vm_deletion": True,
        "image": os.environ["CONDERE_SMOL_IMAGE"],
        "model_invoked": False,
    }
    (destination / "live-inspection.json").write_text(json.dumps(record, indent=2) + "\n")
