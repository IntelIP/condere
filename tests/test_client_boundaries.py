"""Client transport/credential boundaries; no providers or external services."""

import hashlib
import runpy
from pathlib import Path

import httpx
import pytest

from condere.interfaces.mcp import AssessmentClient, validate_api_url
from condere.interfaces.server import configuration


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
    with pytest.raises(ValueError, match="CONDERE_OPENROUTER_API_KEY"):
        configuration(values)
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
