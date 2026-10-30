"""Real stdio MCP and loopback HTTP, with PostgreSQL and simulated research/objects."""

import asyncio
import base64
import hashlib
import json
import os
import socket
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from langsmith.utils import LangSmithNotFoundError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_api import GateCoordinator
from test_coordinator import native_case as native_case
from test_coordinator import publish_spinoff
from test_service import setup_case as setup_case

from condere.interfaces.api import create_app
from condere.runtime.state import content_hash
from condere.runtime.tracing import Tracing

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]


@asynccontextmanager
async def serve(app):
    started = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            started.set()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await asyncio.wait_for(started.wait(), 10)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@asynccontextmanager
async def connect(origin, directory, token="a" * 40):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "condere.interfaces.mcp"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={
            "CONDERE_API_URL": origin,
            "CONDERE_API_TOKEN": token,
            "CONDERE_EXPORT_DIRECTORY": str(directory),
        },
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        yield session


async def call(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    assert not result.isError, result.content
    return result.structuredContent


@pytest.mark.asyncio
async def test_stdio_retrieves_same_saved_result_and_explicit_writes(setup_case, tmp_path):
    service, owner, case = setup_case
    original = "Fictional source: café, no customer evidence.".encode()
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fictional",
        original=original,
        text=original.decode(),
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["No real research."],
    )
    dossier = publish_spinoff(service, owner, case, source.id)
    coordinator = GateCoordinator(service)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40, "other-owner": "b" * 40})
    async with serve(app) as origin:
        async with connect(origin, tmp_path / "exports") as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 12
            assert tools["read_artifact"].annotations.readOnlyHint
            assert tools["read_report"].annotations.readOnlyHint
            assert not tools["admit_assessment"].annotations.readOnlyHint
            assert not tools["export_assessment"].annotations.readOnlyHint
            capabilities = await call(session, "capabilities")
            assert capabilities["result_schemas"] == ["condere.spinoff.v1"]
            listing = await call(session, "list_assessments")
            assert case["id"] in {item["id"] for item in listing["assessments"]}
            identity = {"assessment_id": case["id"]}
            assert await call(session, "read_report", identity) == dossier.model_dump(mode="json")
            reasoning = await call(
                session, "read_artifact", identity | {"ref": "artifacts/reasoning.md"}
            )
            assert reasoning["sha256"] == dossier.reasoning.full.sha256
            assert "Case against" in reasoning["text"]
            escaped = await session.call_tool(
                "read_artifact", identity | {"ref": "../dossier/final"}
            )
            assert escaped.isError
            assert (await call(session, "assessment_status", identity))["status"] == "completed"
            assert (await call(session, "list_sources", identity))["sources"][0]["id"] == source.id
            evidence = identity | {"source_id": source.id}
            passage = await call(session, "read_source", evidence | {"start": 18, "limit": 10})
            assert passage["citation"]["quote"] == original.decode()[18:28]
            body = await call(session, "read_original", evidence | {"start": 18, "limit": 10})
            assert base64.b64decode(body["data_base64"]) == original[18:28]
            assert body["next_offset"] == 28
            assert body["sha256"] == hashlib.sha256(original).hexdigest()
            exported = await call(session, "export_assessment", identity)
            saved = Path(exported["path"])
            assert saved.stat().st_mode & 0o077 == 0
            assert hashlib.sha256(saved.read_bytes()).hexdigest() == exported["sha256"]
            with zipfile.ZipFile(saved) as archive:
                assert original in [archive.read(name) for name in archive.namelist()]
            assert await call(session, "export_assessment", identity) == exported
            assert service.state.operations(owner, case["id"]) == []
            assert coordinator.calls == []

        # A fresh client connection reads the same result; another owner cannot read its objects.
        async with connect(origin, tmp_path / "exports") as session:
            assert await call(session, "read_report", identity) == dossier.model_dump(mode="json")
        before = service.evidence.client.calls
        async with connect(origin, tmp_path / "other", "b" * 40) as session:
            result = await session.call_tool("read_original", evidence)
            assert result.isError and "404" in str(result.content)
        assert service.evidence.client.calls == before

        campaign = str(uuid4())
        service.state.authorize_campaign(owner, campaign, 10000, {"basis": "MCP simulation"})
        async with connect(origin, tmp_path / "exports") as session:
            admission = {
                "campaign_id": campaign,
                "request_key": "mcp-original",
                "brief": {"original_input": "Fictional MCP admission."},
            }
            created = await call(session, "admit_assessment", admission)
            await asyncio.wait_for(coordinator.started.wait(), 5)
            assert (await call(session, "admit_assessment", admission))["id"] == created["id"]
            denied = await session.call_tool(
                "admit_assessment",
                admission
                | {
                    "brief": admission["brief"] | {"cap_microusd": 99999999},
                },
            )
            assert denied.isError
            assert len(coordinator.calls) == 1
            stopped = await call(session, "cancel_assessment", {"assessment_id": created["id"]})
            assert stopped["status"] == "cancelled"
            assert service.state.operations(owner, created["id"]) == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CONDERE_TEST_SANDBOX") != "1" or os.environ.get("CONDERE_TEST_OBJECTS") != "1",
    reason="This complete protocol check requires native VMs and native object storage",
)
async def test_mcp_native_pipeline_and_object_store(native_case, monkeypatch, tmp_path, request):
    service, owner, case, make, _, calls, created, *_ = native_case
    tracing = None
    trace_ids = []
    outage = os.environ.get("CONDERE_TEST_LANGSMITH_OUTAGE") == "1"
    if os.environ.get("CONDERE_TEST_LANGSMITH") == "1" or outage:
        # Integration traces go to the condere agent's local environment (condere-local).
        environment = dict(os.environ) | {
            "LANGSMITH_AGENT_ID": "condere",
            "LANGSMITH_AGENT_ENVIRONMENT": "local",
        }
        environment.pop("LANGSMITH_PROJECT", None)
        if outage:
            environment |= {
                "LANGSMITH_API_KEY": "lsv2-outage",
                "LANGSMITH_ENDPOINT": "http://127.0.0.1:9",
            }
        tracing = Tracing.from_environment(environment, release=service.method_version)
        request.addfinalizer(tracing.close)
    coordinator = make(tracing=tracing)
    completed = asyncio.Event()
    run = coordinator.run

    async def execute(*args, **kwargs):
        result = await run(*args, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(coordinator, "run", execute)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40})
    async with serve(app) as origin, connect(origin, tmp_path / "exports") as session:
        admitted = await call(
            session,
            "admit_assessment",
            {
                "campaign_id": case["campaign_id"],
                "request_key": case["request_key"],
                "brief": case["brief"],
            },
        )
        assert admitted["id"] == case["id"]
        await asyncio.wait_for(completed.wait(), timeout=600)
        identity = {"assessment_id": case["id"]}
        report = await call(session, "read_report", identity)
        assert report == service.report(owner, case["id"]).model_dump(mode="json")
        assert report["schema_version"] == "condere.spinoff.v1"
        sources = await call(session, "list_sources", identity)
        assert sources["total"] == 1
        source = next(
            item for item in sources["sources"] if item["origin"].endswith("native-fixture")
        )
        original = await call(session, "read_original", identity | {"source_id": source["id"]})
        body = base64.b64decode(original["data_base64"])
        assert body == b"Operators report upgrades keep breaking in production."
        archive = await call(session, "export_assessment", identity)
        with zipfile.ZipFile(archive["path"]) as package:
            assert body in [package.read(name) for name in package.namelist()]
            assert "README.md" in package.namelist()
            for link in report["artifacts"]:
                assert hashlib.sha256(package.read(link["ref"])).hexdigest() == link["sha256"]
            preview = Path(".artifacts/report-preview")
            preview.mkdir(parents=True, exist_ok=True)
            for name in package.namelist():
                target = preview / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(package.read(name))
        assert sorted(role for role, _ in calls) == ["code-analyst", "extractor", "lead", "planner"]
        assert len(created) == 2
        assert service.state.operations(owner, case["id"]) == []
        failure = None
        if tracing is not None:
            status = await call(session, "assessment_status", identity)
            records = [item["payload"] for item in status["telemetry"]]
            assert {item["role"] for item in records} == {"lead", "planner"}
            for item in records:
                assert item["execution_mode"] == "simulation"
                if outage:
                    assert item["status"] == "unavailable" and item["error_type"]
                else:
                    assert item["status"] in {"observed", "pending"}
            trace_ids = [item["trace_id"] for item in records]
        if tracing is not None and not outage:
            keys = {
                item["id"]: item["worker_key"] for item in service.state.workers(owner, case["id"])
            }
            technical = next(
                item["trace_id"] for item in records if keys[item["worker_id"]] == "lead"
            )
            deadline = asyncio.get_running_loop().time() + 60
            roots = []
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.to_thread(tracing.client.flush, 5)
                try:
                    roots = await asyncio.to_thread(
                        lambda: [tracing.client.read_run(trace_id) for trace_id in trace_ids]
                    )
                except LangSmithNotFoundError:  # Ingestion lags; poll until the deadline.
                    await asyncio.sleep(1)
                    continue
                runs = await asyncio.to_thread(
                    lambda: list(
                        tracing.client.list_runs(
                            project_name="condere-local",
                            trace_id=technical,
                            filter='eq(name, "submit_analysis")',
                        )
                    )
                )
                failure = next(
                    (run for run in runs if "missing" in str(run.error or run.outputs).lower()),
                    None,
                )
                if failure is not None:
                    break
                await asyncio.sleep(1)
            assert {run.extra["metadata"]["role"] for run in roots} == {"lead", "planner"}
            assert all(run.extra["metadata"]["assessment_id"] == case["id"] for run in roots)
            assert failure is not None and str(failure.trace_id) == technical
        from smol import ConnectOptions, Machine

        remaining = {machine.id for machine in Machine.list(ConnectOptions(target="local"))}
        assert not {sandbox.id for sandbox in created} & remaining
        receipt_name = "native-mcp-telemetry-outage.json" if outage else "native-mcp.json"
        receipt = Path(__file__).resolve().parents[2] / ".artifacts/infrastructure" / receipt_name
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "owner_id": owner,
                    "assessment_id": case["id"],
                    "source_id": source["id"],
                    "source_sha256": original["sha256"],
                    "report_hash": content_hash(report),
                    "zip_sha256": archive["sha256"],
                    "machines_deleted": [item.id for item in created],
                    "research": "simulation",
                    "storage": "native MinIO",
                    "transport": "stdio and HTTP",
                    "provider_operations": 0,
                    "langsmith_trace_ids": trace_ids,
                    "recovered_tool_failure_run": str(failure.id) if failure else None,
                    "telemetry_outage": outage,
                },
                indent=2,
            )
            + "\n"
        )
