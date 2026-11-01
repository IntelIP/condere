"""Local setup writes private configuration without contacting Doppler."""

import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "local_config", Path(__file__).resolve().parents[1] / "scripts/local_config.py"
)
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


def test_local_configuration_preserves_existing_bindings_and_rejects_symlinks(tmp_path):
    output = tmp_path / ".env"
    output.write_text("EXA_API_KEY=existing\n")
    args = Namespace(doppler=False, output=output)
    original = {"EXA_API_KEY": "existing", "UNRELATED_SECRET": "never-written"}
    config.save(args, original, original | {"CONDERE_OWNER_ID": "local owner"})
    text = output.read_text()
    assert text.startswith("EXA_API_KEY=existing\n")
    assert "CONDERE_OWNER_ID='local owner'" in text
    assert "UNRELATED_SECRET" not in text
    assert output.stat().st_mode & 0o777 == 0o600
    config.save(args, original, original)
    assert output.read_text() == text
    link = tmp_path / "link"
    link.symlink_to(output)
    with pytest.raises(OSError):
        config.save(Namespace(doppler=False, output=link), {}, {"CONDERE_OWNER_ID": "new"})
    assert output.read_text() == text


def test_environment_example_covers_the_server_contract():
    from condere.interfaces import server

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    bindings = {
        line.split("=", 1)[0]
        for line in example.splitlines()
        if "=" in line and not line.startswith("#")
    }
    server_names = set(
        server.REQUIRED
        + server.PROVIDER
        + server.CHATGPT_CLOUD
        + server.SANDBOX
        + server.DATABASE
        + server.EXECUTOR
        + server.TRACING
    )
    component_names = {
        "CONDERE_EXPORT_DIRECTORY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "CONDERE_PG_ADMIN_PASSWORD",
        "CONDERE_DATABASE_PASSWORD",
        "CONDERE_CHECKPOINT_PASSWORD",
    }
    assert bindings == server_names | component_names


def test_campaign_helper_records_only_explicit_authority(monkeypatch):
    import runpy
    import sys

    from condere.runtime import state

    calls = []

    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            pass

        def authorize_campaign(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(state, "AssessmentStore", Store)
    monkeypatch.setenv("CONDERE_OWNER_ID", "fixture-owner")
    monkeypatch.setenv("CONDERE_DATABASE_URL", "postgresql://fixture")
    monkeypatch.setattr(
        sys,
        "argv",
        ["authorize_campaign.py", "test", "--cap-microusd", "0", "--reason", "No paid calls"],
    )
    script = Path(__file__).resolve().parents[1] / "scripts/authorize_campaign.py"
    runpy.run_path(str(script), run_name="__main__")
    assert calls == [
        (("fixture-owner", "test", 0, {"basis": "No paid calls"}), {"assessment_limit": 1})
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        ["authorize_campaign.py", "test", "--cap-microusd", "-1", "--reason", "invalid"],
    )
    with pytest.raises(SystemExit):
        runpy.run_path(str(script), run_name="__main__")
    assert len(calls) == 1


def test_generated_bindings_replace_blank_template_entries(tmp_path):
    output = tmp_path / ".env"
    output.write_text("# Keep this comment\nCONDERE_API_TOKEN=\nEXA_API_KEY=existing\n")
    args = Namespace(doppler=False, output=output)
    config.save(args, {"CONDERE_API_TOKEN": ""}, {"CONDERE_API_TOKEN": "generated"})
    assert output.read_text() == (
        "# Keep this comment\nCONDERE_API_TOKEN=generated\nEXA_API_KEY=existing\n"
    )
    with pytest.raises(SystemExit, match="--env-file"):
        config.save(args, {}, {"CONDERE_API_TOKEN": "replacement"})
    assert "CONDERE_API_TOKEN=generated" in output.read_text()
