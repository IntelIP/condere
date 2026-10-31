"""Condere HTTP service, local or on Cloud Run; configuration alone never admits research."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from condere.infra.cloud_run import CloudRunJobs
from condere.interfaces.api import create_app
from condere.interfaces.mcp import validate_api_url
from condere.runtime.chatgpt_plan import DEFAULT_HOME, PLAN_CODE_MODEL, PlanCredentials
from condere.runtime.coordinator import AssessmentCoordinator
from condere.runtime.evidence import EvidenceStore, object_client
from condere.runtime.providers import CLAUDE_OPUS
from condere.runtime.sandbox import SmolSandbox
from condere.runtime.sandbox_modal import ModalSandbox
from condere.runtime.service import AssessmentService
from condere.runtime.state import AssessmentStore
from condere.runtime.tracing import Tracing

REPOSITORY = Path(__file__).resolve().parents[3]

REQUIRED = (
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
    "CONDERE_ARTIFACT_DIRECTORY",
    "EXA_API_KEY",
    "LANGSMITH_API_KEY",
    # Claude subagents and the Jev decision run through OpenRouter in every mode.
    "CONDERE_OPENROUTER_API_KEY",
)
# Lead and planner billing: CONDERE_MODEL_PROVIDER=chatgpt (default, the operator's ChatGPT
# plan via Sign in with ChatGPT) or openrouter. APIFY_TOKEN enables X, Reddit and YouTube.
PROVIDER = ("CONDERE_MODEL_PROVIDER", "CONDERE_CHATGPT_HOME", "APIFY_TOKEN", "GITHUB_TOKEN")
# Code sandbox: CONDERE_SANDBOX=smol (default, local SmolVM with CONDERE_SMOL_IMAGE) or modal
# (Modal credentials from MODAL_TOKEN_ID and MODAL_TOKEN_SECRET or the local Modal profile).
SANDBOX = ("CONDERE_SANDBOX", "CONDERE_SMOL_IMAGE")
# Behind a transaction-mode pooler, CONDERE_DATABASE_DIRECT_URL is the same database's direct
# endpoint, for the coordinator's session lock and schema setup.
DATABASE = ("CONDERE_DATABASE_DIRECT_URL",)
# Where runs execute: CONDERE_EXECUTOR=inprocess (default) or cloud_run, which starts the job
# named by CONDERE_CLOUD_RUN_JOB (projects/<project>/locations/<region>/jobs/<job>) per run.
EXECUTOR = ("CONDERE_EXECUTOR", "CONDERE_CLOUD_RUN_JOB")
# Trace destination: LANGSMITH_PROJECT, or LANGSMITH_AGENT_ID with LANGSMITH_AGENT_ENVIRONMENT.
TRACING = (
    "LANGSMITH_PROJECT",
    "LANGSMITH_AGENT_ID",
    "LANGSMITH_AGENT_ENVIRONMENT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
)


def verified_image(value: str) -> str:
    if re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", value):
        return value
    manifest = REPOSITORY / "infra/smolvm/inspection-image.json"
    try:
        pin = json.loads(manifest.read_text())
        archive = (REPOSITORY / Path(value).expanduser()).resolve()
        expected = (REPOSITORY / pin["archive"]).resolve()
        if archive != expected or not archive.is_file():
            raise ValueError("Select the verified inspection archive or an immutable image digest.")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != pin["archive_sha256"]:
                raise ValueError("The inspection archive differs from its saved content hash.")
    except (OSError, KeyError, json.JSONDecodeError):
        raise ValueError(
            "The local inspection image requires a valid build manifest and archive."
        ) from None
    return str(archive)


def configuration(environment) -> dict[str, str]:
    missing = [name for name in REQUIRED if not environment.get(name)]
    if missing:
        raise ValueError("Missing Condere runtime configuration: " + ", ".join(missing))
    values = {name: environment[name] for name in REQUIRED}
    values |= {
        name: environment[name]
        for name in TRACING + PROVIDER + SANDBOX + DATABASE + EXECUTOR
        if environment.get(name)
    }
    provider = values.setdefault("CONDERE_MODEL_PROVIDER", "chatgpt")
    if provider not in {"chatgpt", "openrouter"}:
        raise ValueError("CONDERE_MODEL_PROVIDER must be chatgpt or openrouter.")
    validate_api_url(values["CONDERE_API_URL"])
    if not values["CONDERE_OWNER_ID"].strip():
        raise ValueError("Configure a nonempty owner identity.")
    token = values["CONDERE_API_TOKEN"]
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError("Configure a bearer token with at least 32 non-whitespace characters.")
    sandbox = values.setdefault("CONDERE_SANDBOX", "smol")
    if sandbox == "smol":
        if not values.get("CONDERE_SMOL_IMAGE"):
            raise ValueError("Missing Condere runtime configuration: CONDERE_SMOL_IMAGE")
        values["CONDERE_SMOL_IMAGE"] = verified_image(values["CONDERE_SMOL_IMAGE"])
    elif sandbox != "modal":
        raise ValueError("CONDERE_SANDBOX must be smol or modal.")
    executor = values.setdefault("CONDERE_EXECUTOR", "inprocess")
    if executor not in {"inprocess", "cloud_run"}:
        raise ValueError("CONDERE_EXECUTOR must be inprocess or cloud_run.")
    if executor == "cloud_run" and not values.get("CONDERE_CLOUD_RUN_JOB"):
        raise ValueError("Missing Condere runtime configuration: CONDERE_CLOUD_RUN_JOB")
    return values


def sandbox_backend(values: dict) -> tuple[type, str]:
    """The sandbox class and its image reference."""
    if values["CONDERE_SANDBOX"] == "smol":
        return SmolSandbox, values["CONDERE_SMOL_IMAGE"]
    manifest = json.loads((REPOSITORY / "infra/smolvm/inspection-image.json").read_text())
    return ModalSandbox, manifest["base_image"]


def build_runtime(values: dict):
    """The service and coordinator, shared by the API and the per-assessment job.

    Returns (service, coordinator, object client, tracing); the caller closes the last two.
    """
    state = AssessmentStore(
        values["CONDERE_DATABASE_URL"], direct_dsn=values.get("CONDERE_DATABASE_DIRECT_URL")
    )
    state.setup()
    objects = object_client(values)
    tracing = None
    try:
        evidence = EvidenceStore(state, objects, values["CONDERE_EVIDENCE_BUCKET"])
        provider, plan = values["CONDERE_MODEL_PROVIDER"], None
        service = AssessmentService(
            state,
            evidence,
            execution_mode="live",
            code_model_id=PLAN_CODE_MODEL if provider == "chatgpt" else CLAUDE_OPUS.id,
        )
        tracing = Tracing.from_environment(values, release=service.method_version)
        if provider == "chatgpt":
            plan = PlanCredentials(Path(values.get("CONDERE_CHATGPT_HOME") or DEFAULT_HOME))
            if not plan.status()["plan_usage"]:
                raise ValueError(
                    "Sign in with ChatGPT first: uv run python scripts/chatgpt_login.py"
                )
        sandboxes, image = sandbox_backend(values)
        coordinator = AssessmentCoordinator(
            service,
            checkpoint_dsn=values["CONDERE_CHECKPOINT_URL"],
            application_dsn=values["CONDERE_DATABASE_URL"],
            image=image,
            sandboxes=sandboxes,
            artifacts=Path(values["CONDERE_ARTIFACT_DIRECTORY"]),
            openrouter_key=values["CONDERE_OPENROUTER_API_KEY"],
            plan_credentials=plan,
            exa_key=values["EXA_API_KEY"],
            apify_token=values.get("APIFY_TOKEN"),
            github_token=values.get("GITHUB_TOKEN"),
            tracing=tracing,
        )
    except BaseException:
        objects.close()
        if tracing is not None:
            tracing.close()
        raise
    return service, coordinator, objects, tracing


def build_app():
    values = configuration(os.environ)
    service, coordinator, objects, tracing = build_runtime(values)
    launcher = (
        CloudRunJobs(values["CONDERE_CLOUD_RUN_JOB"])
        if values["CONDERE_EXECUTOR"] == "cloud_run"
        else None
    )
    try:
        app = create_app(
            service,
            coordinator,
            owner_tokens={
                values["CONDERE_OWNER_ID"]: values["CONDERE_API_TOKEN"],
            },
            launcher=launcher,
        )
    except BaseException:
        objects.close()
        tracing.close()
        raise

    application_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with application_lifespan(app):
                yield
        finally:
            objects.close()
            await asyncio.to_thread(tracing.close)
            if launcher is not None:
                await launcher.close()

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    try:
        values = configuration(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if port := os.environ.get("PORT"):  # Cloud Run sets the port; TLS ends at its front end.
        uvicorn.run(build_app, factory=True, host="0.0.0.0", port=int(port), access_log=False)
        return
    origin = urlsplit(values["CONDERE_API_URL"])
    if origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port:
        raise SystemExit("The local launcher requires http://127.0.0.1:<port>.")
    uvicorn.run(build_app, factory=True, host="127.0.0.1", port=origin.port, access_log=False)


if __name__ == "__main__":
    main()
