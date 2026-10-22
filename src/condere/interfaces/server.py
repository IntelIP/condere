"""Installed local HTTP service; configuration alone never admits paid research."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from condere.interfaces.api import create_app
from condere.interfaces.mcp import validate_api_url
from condere.runtime.coordinator import AssessmentCoordinator
from condere.runtime.evidence import EvidenceStore, object_client
from condere.runtime.service import AssessmentService
from condere.runtime.state import AssessmentStore

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
    "CONDERE_SMOL_IMAGE",
    "CONDERE_ARTIFACT_DIRECTORY",
    "CONDERE_OPENROUTER_API_KEY",
    "EXA_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
)


def configuration(environment) -> dict[str, str]:
    missing = [name for name in REQUIRED if not environment.get(name)]
    if missing:
        raise ValueError("Missing Condere runtime configuration: " + ", ".join(missing))
    values = {name: environment[name] for name in REQUIRED}
    validate_api_url(values["CONDERE_API_URL"])
    validate_api_url(values["LANGFUSE_BASE_URL"])
    if not values["CONDERE_OWNER_ID"].strip():
        raise ValueError("Configure a nonempty owner identity.")
    token = values["CONDERE_API_TOKEN"]
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError("Configure a bearer token with at least 32 non-whitespace characters.")
    if "@sha256:" not in values["CONDERE_SMOL_IMAGE"]:
        raise ValueError("Configure an immutable SmolVM image digest.")
    return values


def build_app():
    values = configuration(os.environ)
    state = AssessmentStore(values["CONDERE_DATABASE_URL"])
    state.setup()
    objects = object_client(values)
    telemetry = None
    try:
        evidence = EvidenceStore(state, objects, values["CONDERE_EVIDENCE_BUCKET"])
        service = AssessmentService(state, evidence, execution_mode="live")
        telemetry = Langfuse(
            public_key=values["LANGFUSE_PUBLIC_KEY"],
            secret_key=values["LANGFUSE_SECRET_KEY"],
            base_url=values["LANGFUSE_BASE_URL"],
            environment="development",
            release=service.method_version,
        )
        coordinator = AssessmentCoordinator(
            service,
            checkpoint_dsn=values["CONDERE_CHECKPOINT_URL"],
            application_dsn=values["CONDERE_DATABASE_URL"],
            image=values["CONDERE_SMOL_IMAGE"],
            artifacts=Path(values["CONDERE_ARTIFACT_DIRECTORY"]),
            openrouter_key=values["CONDERE_OPENROUTER_API_KEY"],
            exa_key=values["EXA_API_KEY"],
            callbacks=[CallbackHandler(public_key=values["LANGFUSE_PUBLIC_KEY"])],
        )
        app = create_app(
            service,
            coordinator,
            owner_tokens={
                values["CONDERE_OWNER_ID"]: values["CONDERE_API_TOKEN"],
            },
        )
    except BaseException:
        objects.close()
        if telemetry is not None:
            telemetry.shutdown()
        raise

    application_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with application_lifespan(app):
                yield
        finally:
            objects.close()
            await asyncio.to_thread(telemetry.shutdown)

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    try:
        values = configuration(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    origin = urlsplit(values["CONDERE_API_URL"])
    if origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port:
        raise SystemExit("The local launcher requires http://127.0.0.1:<port>.")
    uvicorn.run(build_app, factory=True, host="127.0.0.1", port=origin.port, access_log=False)


if __name__ == "__main__":
    main()
