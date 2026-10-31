"""Authenticated assessment HTTP interface; saved retrieval never starts research."""

import asyncio
import hashlib
import hmac
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from starlette.background import BackgroundTask

from condere.runtime.bundle import render_readme
from condere.runtime.calculations import Record
from condere.runtime.chatgpt_plan import USAGE_URL
from condere.runtime.contracts import IntakeBrief
from condere.runtime.coordinator import run_recorded
from condere.runtime.evidence import EvidenceError
from condere.runtime.service import AssessmentService
from condere.runtime.state import NotFound, StateError

PREFIX = "/agents/condere/assessments"


class Admission(Record):
    campaign_id: str = Field(min_length=1, max_length=200)
    request_key: str = Field(min_length=1, max_length=200)
    brief: IntakeBrief


def create_app(
    service: AssessmentService, coordinator, *, owner_tokens: dict[str, str], launcher=None
):
    """`launcher(owner, assessment_id, resume)` starts runs elsewhere (one cloud job each);
    without one, runs execute as tasks in this process."""
    """Credentials come from trusted configuration, never an intake body or a model."""
    if coordinator.service is not service:
        raise ValueError("HTTP and worker execution must use the same assessment service.")
    if not owner_tokens or any(
        not owner or len(token) < 32 for owner, token in owner_tokens.items()
    ):
        raise ValueError(
            "Configure an owner and a distinct bearer token of at least 32 characters."
        )
    digests = [
        (owner, hashlib.sha256(token.encode()).digest()) for owner, token in owner_tokens.items()
    ]
    if len({digest for _, digest in digests}) != len(digests):
        raise ValueError("Owners cannot share an authentication token.")
    jobs: dict[tuple[str, str], asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            active = list(jobs.values())
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    app = FastAPI(
        title="Condere", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is not None and credentials.scheme.lower() == "bearer":
            digest = hashlib.sha256(credentials.credentials.encode()).digest()
            for owner, expected in digests:
                if hmac.compare_digest(digest, expected):
                    return owner
        raise HTTPException(
            401, "Valid owner credentials are required.", headers={"WWW-Authenticate": "Bearer"}
        )

    Owner = Annotated[str, Depends(authenticate)]

    @app.exception_handler(StateError)
    async def state_error(request, error):
        return JSONResponse(
            status_code=404 if isinstance(error, NotFound) else 409,
            content={"code": type(error).__name__, "detail": str(error)},
        )

    @app.exception_handler(EvidenceError)
    async def evidence_error(request, error):
        return JSONResponse(
            status_code=503, content={"code": "EvidenceUnavailable", "detail": str(error)}
        )

    async def execute(owner, assessment_id, resume):
        try:
            await run_recorded(coordinator, service.state, owner, assessment_id, resume=resume)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Recorded on the assessment; the HTTP request that started it has returned.
        finally:
            jobs.pop((owner, assessment_id), None)

    async def launch(owner, assessment_id, *, resume=False):
        if launcher is not None:
            await launcher(owner, assessment_id, resume)
            return
        key = (owner, assessment_id)
        if key not in jobs:
            jobs[key] = asyncio.create_task(execute(owner, assessment_id, resume))

    @app.get(PREFIX + "/capabilities")
    async def capabilities(owner: Owner):
        return {
            "agent_id": "condere",
            "execution_mode": service.execution_mode,
            "policy_version": service.policy_version,
            "method_version": service.method_version,
            "flow": ["repository_index", "lead", "decision", "build_plan"],
            "result_schemas": ["condere.spinoff.v1"],
            "artifacts": "read_artifact by the ref listed in the report's artifacts",
            # Clients show "Using ChatGPT plan" with a Manage usage link when this is set.
            "model_billing": (
                {"type": "chatgpt_plan", "manage_usage": USAGE_URL}
                if getattr(coordinator, "plan_credentials", None) is not None
                else {"type": "provider_account"}
            ),
        }

    @app.post(PREFIX, status_code=202)
    async def create(body: Admission, owner: Owner):
        case = await asyncio.to_thread(
            service.admit,
            owner,
            body.campaign_id,
            body.request_key,
            body.brief.model_dump(mode="json"),
        )
        if case["status"] == "admitted":
            await launch(owner, case["id"])
        return case

    @app.get(PREFIX)
    async def list_assessments(
        owner: Owner,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=50),
    ):
        return await asyncio.to_thread(service.state.list_assessments, owner, offset, limit)

    @app.get(PREFIX + "/{assessment_id}")
    async def status(assessment_id: str, owner: Owner):
        case = await asyncio.to_thread(service.state.get, owner, assessment_id)
        return case | {
            "workers": await asyncio.to_thread(service.state.workers, owner, assessment_id),
            "spending": await asyncio.to_thread(service.state.spending, owner, assessment_id),
            "execution_errors": await asyncio.to_thread(
                service.state.records, owner, assessment_id, kind="execution_error"
            ),
            "telemetry": await asyncio.to_thread(
                service.state.records, owner, assessment_id, kind="telemetry"
            ),
        }

    @app.post(PREFIX + "/{assessment_id}/cancel")
    async def cancel(assessment_id: str, owner: Owner):
        return await coordinator.cancel(owner, assessment_id)

    @app.post(PREFIX + "/{assessment_id}/continue", status_code=202)
    async def continue_assessment(assessment_id: str, owner: Owner):
        case = await asyncio.to_thread(service.state.get, owner, assessment_id)
        if case["status"] != "completed":
            await launch(owner, assessment_id, resume=True)
        return case

    @app.get(PREFIX + "/{assessment_id}/report")
    async def report(
        assessment_id: str, owner: Owner, format: Literal["json", "markdown"] = "json"
    ):
        saved = await asyncio.to_thread(service.report, owner, assessment_id)
        if format == "markdown":
            return PlainTextResponse(render_readme(saved), media_type="text/markdown")
        return saved.model_dump(mode="json")

    @app.get(PREFIX + "/{assessment_id}/artifacts/{ref:path}")
    async def artifact(
        assessment_id: str,
        ref: str,
        owner: Owner,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20_000, ge=1, le=100_000),
    ):
        return await asyncio.to_thread(
            service.artifact, owner, assessment_id, ref, offset=offset, limit=limit
        )

    @app.get(PREFIX + "/{assessment_id}/sources")
    async def sources(
        assessment_id: str,
        owner: Owner,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=50),
    ):
        records = await asyncio.to_thread(
            service.state.records, owner, assessment_id, kind="source"
        )
        return {
            "sources": [item["payload"] for item in records[offset : offset + limit]],
            "next_offset": offset + limit if offset + limit < len(records) else None,
            "total": len(records),
        }

    @app.get(PREFIX + "/{assessment_id}/sources/{source_id}")
    async def source(
        assessment_id: str,
        source_id: str,
        owner: Owner,
        start: int = Query(default=0, ge=0),
        limit: int = Query(default=4000, ge=1, le=12000),
    ):
        try:
            return await asyncio.to_thread(
                service.evidence.passage, owner, assessment_id, source_id, start, limit
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get(PREFIX + "/{assessment_id}/sources/{source_id}/original")
    async def original(assessment_id: str, source_id: str, owner: Owner):
        metadata = await asyncio.to_thread(service.evidence.source, owner, assessment_id, source_id)
        body = await asyncio.to_thread(service.evidence.original, owner, assessment_id, source_id)
        return Response(
            body,
            media_type=metadata.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{metadata.id}.bin"',
                "X-Content-Type-Options": "nosniff",
                "ETag": '"' + metadata.original_sha256 + '"',
            },
        )

    @app.get(PREFIX + "/{assessment_id}/export")
    async def export(assessment_id: str, owner: Owner):
        await asyncio.to_thread(service.state.get, owner, assessment_id)
        directory = Path(tempfile.mkdtemp(prefix="condere-export-"))
        destination = directory / "assessment.zip"
        try:
            await asyncio.to_thread(service.export, owner, assessment_id, destination)
        except BaseException:
            shutil.rmtree(directory)
            raise
        return FileResponse(
            destination,
            media_type="application/zip",
            filename="condere-assessment.zip",
            background=BackgroundTask(shutil.rmtree, directory),
        )

    return app
