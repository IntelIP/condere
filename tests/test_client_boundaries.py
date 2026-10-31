"""Client transport/credential boundaries; no providers or external services."""

import hashlib
import json
import runpy
from pathlib import Path

import httpx
import pytest

from condere.interfaces import server
from condere.interfaces.mcp import AssessmentClient, validate_api_url
from condere.interfaces.server import configuration
from condere.runtime.evidence import object_client
from condere.runtime.state import AssessmentStore


def test_runtime_accepts_only_pinned_or_verified_local_images(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REPOSITORY", tmp_path)
    archive = tmp_path / ".runtime/images/inspection.tar"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"fixture image archive")
    manifest = tmp_path / "infra/smolvm/inspection-image.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "archive": str(archive.relative_to(tmp_path)),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
    )
    assert server.verified_image(str(archive)) == str(archive)
    assert server.verified_image(str(archive.relative_to(tmp_path))) == str(archive)
    immutable = "python@sha256:" + "a" * 64
    assert server.verified_image(immutable) == immutable
    for value in ("python:latest", "python@sha256:", str(tmp_path / "other.tar")):
        with pytest.raises(ValueError, match="verified inspection"):
            server.verified_image(value)
    archive.write_bytes(b"changed archive")
    with pytest.raises(ValueError, match="content hash"):
        server.verified_image(str(archive))


def test_launch_selects_only_component_credentials_and_rejects_unsafe_origins():
    launcher = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run_local.py"))
    select = launcher["child_environment"]
    selected = {
        "CONDERE_API_URL": "http://127.0.0.1:7778",
        "CONDERE_API_TOKEN": "t" * 40,
        "CONDERE_EXPORT_DIRECTORY": "/private/exports",
    }
    values = selected | {"OPENROUTER_API_KEY": "secret", "CONDERE_DATABASE_URL": "secret"}
    inherited = {
        "PATH": "/bin",
        "HOME": "/home/operator",
        "EXA_API_KEY": "secret",
        "DOPPLER_TOKEN": "secret",
        "LANGSMITH_API_KEY": "secret",
    }
    environment = select("mcp", values, inherited)
    assert {key: environment[key] for key in selected} == selected
    assert not any(
        key in environment
        for key in (
            "EXA_API_KEY",
            "DOPPLER_TOKEN",
            "LANGSMITH_API_KEY",
            "CONDERE_DATABASE_URL",
            "OPENROUTER_API_KEY",
        )
    )
    assert environment["LANGSMITH_TRACING"] == "false"
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        configuration(values)
    complete = (
        values
        | {
            name: "configured"
            for name in (
                "CONDERE_OWNER_ID",
                "CONDERE_CHECKPOINT_URL",
                "CONDERE_EVIDENCE_ENDPOINT",
                "CONDERE_EVIDENCE_REGION",
                "CONDERE_EVIDENCE_ACCESS_KEY",
                "CONDERE_EVIDENCE_SECRET_KEY",
                "CONDERE_EVIDENCE_BUCKET",
                "CONDERE_ARTIFACT_DIRECTORY",
                "EXA_API_KEY",
                "LANGSMITH_API_KEY",
            )
        }
        | {"CONDERE_SMOL_IMAGE": "registry.test/condere@sha256:" + "0" * 64}
    )
    # Claude subagents and Jev need OpenRouter even when the lead uses the ChatGPT plan.
    with pytest.raises(ValueError, match="CONDERE_OPENROUTER_API_KEY"):
        configuration(complete)
    complete |= {"CONDERE_OPENROUTER_API_KEY": "configured"}
    assert configuration(complete)["CONDERE_MODEL_PROVIDER"] == "chatgpt"
    with pytest.raises(ValueError, match="chatgpt or openrouter"):
        configuration(complete | {"CONDERE_MODEL_PROVIDER": "other"})
    assert configuration(complete)["CONDERE_SANDBOX"] == "smol"
    # Modal builds the inspection image itself, so it needs no local SmolVM archive.
    without_archive = {k: v for k, v in complete.items() if k != "CONDERE_SMOL_IMAGE"}
    assert configuration(without_archive | {"CONDERE_SANDBOX": "modal"})["CONDERE_SANDBOX"] == (
        "modal"
    )
    with pytest.raises(ValueError, match="CONDERE_SMOL_IMAGE"):
        configuration(without_archive)
    with pytest.raises(ValueError, match="smol or modal"):
        configuration(complete | {"CONDERE_SANDBOX": "other"})
    assert configuration(complete)["CONDERE_EXECUTOR"] == "inprocess"
    with pytest.raises(ValueError, match="CONDERE_CLOUD_RUN_JOB"):
        configuration(complete | {"CONDERE_EXECUTOR": "cloud_run"})
    with pytest.raises(ValueError, match="inprocess or cloud_run"):
        configuration(complete | {"CONDERE_EXECUTOR": "other"})
    for url in (
        "http://remote.test",
        "https://user:pass@remote.test",
        "https://x.test?key=value",
        "https://x.test/path",
        "https://x.test#fragment",
        "file:///tmp/data",
    ):
        with pytest.raises(ValueError):
            validate_api_url(url)


@pytest.mark.asyncio
async def test_transport_rejects_redirects_changed_originals_and_failed_exports(tmp_path):
    requests = []
    mode = "redirect"

    class FailedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial export"
            raise httpx.ReadError("simulated private upstream details")

    async def reply(request):
        requests.append(request)
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.test/private"})
        if mode == "original":
            return httpx.Response(200, content=b"changed original", headers={"ETag": '"wrong"'})
        if mode == "export":
            return httpx.Response(
                200, stream=FailedBody(), headers={"Content-Type": "application/zip"}
            )
        return httpx.Response(
            200, content=b"fictional zip", headers={"Content-Type": "application/zip"}
        )

    client = AssessmentClient(
        "https://condere.test", "a" * 40, tmp_path / "exports", transport=httpx.MockTransport(reply)
    )
    try:
        with pytest.raises(ValueError, match="307"):
            await client.request("GET", "/capabilities")
        assert len(requests) == 1
        for identity in ("../capabilities", "%2e%2e", "/other", ".."):
            with pytest.raises(ValueError, match="identifier"):
                await client.original(identity, "source", 0, 10)
        assert len(requests) == 1
        mode = "original"
        with pytest.raises(ValueError, match="content identity"):
            await client.original("assessment", "source", 0, 10)
        mode = "export"
        with pytest.raises(ValueError, match="download failed"):
            await client.export("assessment")
        assert list(client.export_directory.iterdir()) == []
        mode = "saved"
        expected = client.export_directory / (hashlib.sha256(b"fictional zip").hexdigest() + ".zip")
        expected.write_bytes(b"changed previously saved file")
        with pytest.raises(ValueError, match="not overwritten"):
            await client.export("assessment")
        assert expected.read_bytes() == b"changed previously saved file"
        assert list(client.export_directory.iterdir()) == [expected]
    finally:
        await client.http.aclose()


def test_cloud_storage_and_pooled_database_settings():
    client = object_client(
        {
            "CONDERE_EVIDENCE_ENDPOINT": "https://account.r2.cloudflarestorage.com",
            "CONDERE_EVIDENCE_REGION": "auto",
            "CONDERE_EVIDENCE_ACCESS_KEY": "synthetic-not-a-credential",
            "CONDERE_EVIDENCE_SECRET_KEY": "synthetic-not-a-credential",
        }
    )
    # R2 rejects botocore's default CRC checksums.
    assert client.meta.config.request_checksum_calculation == "when_required"
    assert client.meta.config.response_checksum_validation == "when_required"
    pooled = "postgresql://app@project-pooler.example/condere"
    direct = "postgresql://app@project.example/condere"
    # The coordinator's session lock and schema setup bypass a transaction-mode pooler.
    assert AssessmentStore(pooled, direct_dsn=direct)._direct_dsn == direct
    assert AssessmentStore(pooled)._direct_dsn == pooled
