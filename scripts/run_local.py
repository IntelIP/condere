"""Load only the selected component's configuration from the dedicated Doppler config."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

MCP_NAMES = ("CONDERE_API_URL", "CONDERE_API_TOKEN", "CONDERE_EXPORT_DIRECTORY")
API_NAMES = (
    "CONDERE_API_URL",
    "CONDERE_API_TOKEN",
    "CONDERE_OWNER_ID",
    "CONDERE_DATABASE_URL",
    "CONDERE_CHECKPOINT_URL",
    "CONDERE_EVIDENCE_ENDPOINT",
    "CONDERE_EVIDENCE_REGION",
    "CONDERE_EVIDENCE_ACCESS_KEY",
    "CONDERE_EVIDENCE_SECRET_KEY",
    "CONDERE_EVIDENCE_BUCKET",
    "CONDERE_SMOL_IMAGE",
    "CONDERE_ARTIFACT_DIRECTORY",
    "EXA_API_KEY",
    "LANGSMITH_API_KEY",
    "CONDERE_OPENROUTER_API_KEY",
)
# Optional LangSmith destination and endpoint; the server validates the combination.
API_OPTIONAL = (
    "CONDERE_MODEL_PROVIDER",
    "CONDERE_CHATGPT_HOME",
    "APIFY_TOKEN",
    "GITHUB_TOKEN",
    "CONDERE_SANDBOX",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "LANGSMITH_PROJECT",
    "LANGSMITH_AGENT_ID",
    "LANGSMITH_AGENT_ENVIRONMENT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
)


def child_environment(component: str, values: dict, inherited: dict) -> dict:
    names = MCP_NAMES if component == "mcp" else API_NAMES
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise ValueError("Missing Doppler configuration: " + ", ".join(missing))
    result = {
        name: inherited[name]
        for name in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
        if name in inherited
    }
    result.update({name: values[name] for name in names})
    if component == "api":
        result.update({name: values[name] for name in API_OPTIONAL if values.get(name)})
    result.update({"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
    if component == "api":
        result["LANGSMITH_REVISION_ID"] = revision()
    return result


def revision() -> str:
    """Code version for traces, so trace analysis can tie behavior to the code that ran."""
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    return (commit or "unknown") + ("-dirty" if dirty else "")


def main() -> None:
    from doppler_config import doppler

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("api", "mcp"))
    args = parser.parse_args()
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv before starting Condere.")
    if not shutil.which("doppler"):
        raise SystemExit(
            "Install Doppler and reuse the configured identity before starting Condere."
        )
    values = json.loads(doppler("secrets", "download", "--no-file", "--format", "json"))
    try:
        environment = child_environment(args.component, values, os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    root = str(Path(__file__).resolve().parents[1])
    module = "condere.interfaces.mcp" if args.component == "mcp" else "condere.interfaces.server"
    os.execve(uv, [uv, "--directory", root, "run", "--frozen", "python", "-m", module], environment)


if __name__ == "__main__":
    main()
