"""Start a component with only its configuration; use uv --env-file for local files."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

# Keep the launcher and server on the same configuration contract.
from condere.interfaces.server import (
    CHATGPT_CLOUD,
    DATABASE,
    EXECUTOR,
    PROVIDER,
    REQUIRED,
    SANDBOX,
    TRACING,
)

MCP_NAMES = ("CONDERE_API_URL", "CONDERE_API_TOKEN", "CONDERE_EXPORT_DIRECTORY")
API_NAMES = REQUIRED
API_OPTIONAL = (
    PROVIDER
    + CHATGPT_CLOUD
    + SANDBOX
    + DATABASE
    + EXECUTOR
    + TRACING
    + (
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
    )
)


def child_environment(component: str, values: dict, inherited: dict) -> dict:
    names = MCP_NAMES if component == "mcp" else API_NAMES
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise ValueError("Missing Condere configuration: " + ", ".join(missing))
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("api", "mcp"))
    parser.add_argument(
        "--doppler", action="store_true", help="Read the configured Doppler project."
    )
    args = parser.parse_args()
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv before starting Condere.")
    values = dict(os.environ)
    if args.doppler:
        result = subprocess.run(
            ["doppler", "secrets", "download", "--no-file", "--format", "json"],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise SystemExit("Doppler configuration could not be read; check your local setup.")
        values = json.loads(result.stdout)
    try:
        environment = child_environment(args.component, values, os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    root = str(Path(__file__).resolve().parents[1])
    module = "condere.interfaces.mcp" if args.component == "mcp" else "condere.interfaces.server"
    os.execve(uv, [uv, "--directory", root, "run", "--frozen", "python", "-m", module], environment)


if __name__ == "__main__":
    main()
