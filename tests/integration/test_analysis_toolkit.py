"""Real offline analysis with explicitly fictional inputs."""

import json
import os
from uuid import uuid4

import pytest

from condere.runtime.agent import method_files
from condere.runtime.sandbox import SmolSandbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_SANDBOX") != "1", reason="Native sandbox opt-in required"
    ),
]


def test_graph_navigation_and_cache(tmp_path):
    sandbox = SmolSandbox.create(
        "condere-toolkit-" + uuid4().hex[:16], os.environ["CONDERE_SMOL_IMAGE"], tmp_path
    )
    try:
        files = method_files()
        files["/workspace/repository/src/app.py"] = (
            b"def helper():\n    return 42\n\ndef main():\n    return helper()\n"
        )
        files["/workspace/repository/src/example.cpp"] = b"int value() { return 42; }\n"
        assert not any(item.error for item in sandbox.upload_files(list(files.items())))
        command = (
            "python /workspace/skills/code-exploration/scripts/explore.py "
            "/workspace/repository --scope src --question 'main helper'"
        )
        first = sandbox.execute(command)
        assert first.exit_code == 0, first.output
        result = json.loads(first.output)
        assert "calls" in result["navigation"] and "helper" in result["navigation"]
        assert result["files"] == 2 and not result["reused"]
        assert json.loads(sandbox.execute(command).output)["reused"]
        rejected = sandbox.execute(command.replace("--scope src", "--scope ../"))
        assert rejected.exit_code != 0
    finally:
        sandbox.close(delete=True)


FIXTURE_REPOSITORY = {
    "Dockerfile": "FROM python:3.12-slim\nENV APP_PORT=8080\nEXPOSE 8080\n"
    'CMD ["python", "-m", "service"]\n',
    "docker-compose.yml": "services:\n  api:\n    build: .\n  db:\n    image: postgres:16\n",
    "deploy/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
    "infra/main.tf": 'provider "aws" {}\nresource "aws_s3_bucket" "data" {}\n',
    ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: x\n",
    "LICENSE": "MIT License\n\nPermission is hereby granted, free of charge, to any person\n",
    "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.1.0"\n'
    'dependencies = ["psycopg>=3", "redis", "fastapi"]\n',
    "README.md": "# Fixture service\n",
    "service/__main__.py": "import os\nfrom service.store import save\n\n"
    "def main():\n    save(os.environ['DATABASE_URL'])\n\n"
    "if __name__ == '__main__':\n    main()\n",
    "service/store.py": "def save(url):\n    return url\n",
}


def test_repository_index_extracts_deployment_facts_and_a_queryable_graph(tmp_path):
    import hashlib
    import io
    import tarfile

    from condere.runtime.indexing import build_index
    from condere.runtime.repositories import RepositorySnapshot, _manifest

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, text in FIXTURE_REPOSITORY.items():
            data = text.encode()
            info = tarfile.TarInfo("fixture-root/" + name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    snapshot = RepositorySnapshot(
        "https://github.com/example/fixture",
        "a" * 40,
        archive,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        _manifest(archive),
    )
    sandbox = SmolSandbox.create(
        "condere-index-test-" + uuid4().hex[:12], os.environ["CONDERE_SMOL_IMAGE"], tmp_path
    )
    try:
        files = build_index(snapshot, sandbox)
        facts = json.loads(files["facts.json"])
        deploy = facts["deployment"]
        assert deploy["dockerfiles"][0]["exposed_ports"] == ["8080"]
        assert deploy["compose"][0]["services"] == ["api", "db"]
        assert deploy["compose"][0]["images"] == ["postgres:16"]
        assert deploy["kubernetes_kinds"] == {"Deployment": 1}
        assert deploy["terraform"] == {"provider:aws": 1, "resource:aws_s3_bucket": 1}
        assert facts["licenses"]["files"][0]["identified"] == ["MIT"]
        assert facts["dependency_signals"]["PostgreSQL"] == ["psycopg"]
        assert facts["dependency_signals"]["Redis"] == ["redis"]
        assert "DATABASE_URL" in facts["environment_variables"]
        assert "service/__main__.py" in facts["entry_points"]
        assert facts["ci"][0]["jobs"] == ["test"]
        assert facts["graph"]["built"] and facts["graph"]["nodes"] > 0
        report = files["report.md"].decode()
        assert "ports 8080" in report
        installed = "/workspace/index/fixture"
        assert sandbox.execute(f"mkdir -p {installed}").exit_code == 0
        sandbox.machine.write_file(f"{installed}/graph.json", files["graph.json"])
        query = sandbox.execute(
            f"graphify query 'main save' --graph {installed}/graph.json --budget 400"
        )
        assert query.exit_code == 0 and "save" in query.output, query.output
        hubs = sandbox.execute(f"graphify god-nodes --top 5 --json --graph {installed}/graph.json")
        assert hubs.exit_code == 0 and json.loads(hubs.output)
    finally:
        sandbox.close(delete=True)
