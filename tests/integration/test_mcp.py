"""Real stdio MCP and loopback HTTP, with PostgreSQL and simulated research/objects."""

import asyncio
import base64
import hashlib
import os
import socket
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_api import GateCoordinator
from test_service import ready_for_finalization
from test_service import setup_case as setup_case

from condere.interfaces.api import create_app

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
    ready_for_finalization(service, owner, case)
    dossier = service.finalize(owner, case["id"], case["generation"])
    coordinator = GateCoordinator(service)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40, "other-owner": "b" * 40})
    async with serve(app) as origin:
        async with connect(origin, tmp_path / "exports") as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 11
            assert tools["read_report"].annotations.readOnlyHint
            assert not tools["admit_assessment"].annotations.readOnlyHint
            assert not tools["export_assessment"].annotations.readOnlyHint
            assert len((await call(session, "capabilities"))["questions"]) == 17
            listing = await call(session, "list_assessments")
            assert case["id"] in {item["id"] for item in listing["assessments"]}
            identity = {"assessment_id": case["id"]}
            assert await call(session, "read_report", identity) == dossier.model_dump(mode="json")
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
