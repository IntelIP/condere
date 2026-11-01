"""Reject private operational material in a cleaned public checkout."""

import subprocess
from pathlib import PurePosixPath

PRIVATE_NAMES = {"roadmap.md", "backlog.md", "goal.md", "demo.md", "system_design.md"}
PRIVATE_PREFIXES = ("docs/production", ".claude/routines/", ".claude/skills/condere-improve/")
PRIVATE_PATHS = {
    "agent/references/migration.json",
    "infra/cloudrun/cloudbuild.yaml",
    "infra/README.md",
    "scripts/deploy_cloud_run.py",
    "scripts/doppler_config.py",
    "scripts/chatgpt_cloud_signin.py",
    "scripts/production_mcp.py",
    "scripts/settle_apify.py",
    "scripts/compare_runs.py",
    "scripts/evaluate_runs.py",
    "scripts/run_metrics.py",
    "scripts/trace_digest.py",
}


def forbidden(path):
    return (
        PurePosixPath(path).name in PRIVATE_NAMES
        or path in PRIVATE_PATHS
        or path.startswith(PRIVATE_PREFIXES)
        or PurePosixPath(path).name.startswith(".env")
        and path != ".env.example"
    )


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    bad = sorted(path for path in paths if path and forbidden(path))
    if bad:
        raise SystemExit("Private files cannot enter the public repository:\n" + "\n".join(bad))
    print("Public path boundary passed.")


if __name__ == "__main__":
    main()
