"""Load only the selected component's configuration from the dedicated Doppler config."""

import argparse
import json
import os
import shutil
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
    "CONDERE_OPENROUTER_API_KEY",
    "EXA_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
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
    result.update({"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
    return result


def main() -> None:
    from configure_langfuse import doppler

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
