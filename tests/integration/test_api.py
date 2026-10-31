"""HTTP contracts with real PostgreSQL, fictional findings, and a gated execution test double."""

import asyncio
import hashlib
import io
import os
import zipfile
from uuid import uuid4

import httpx
import pytest
from test_coordinator import native_case as native_case
from test_coordinator import publish_spinoff
from test_service import setup_case as setup_case

from condere.interfaces.api import PREFIX, create_app
from condere.runtime.bundle import render_readme

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONDERE_TEST_POSTGRES") != "1", reason="Explicit PostgreSQL opt-in required"
    ),
]

HEADERS = {"Authorization": "Bearer " + "a" * 40}
OTHER = {"Authorization": "Bearer " + "b" * 40}


class GateCoordinator:
    """Only verifies HTTP task ownership; native execution is covered by coordinator checks."""

    def __init__(self, service):
        self.service = service
        self.started = asyncio.Event()
        self.calls = []
        self.running = None

    async def run(self, owner, assessment_id, *, resume=False):
        self.calls.append((owner, assessment_id, resume))
        self.running = asyncio.current_task()
        try:
            case = self.service.begin(owner, assessment_id, resume=resume)
        finally:
            self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.service.state.stop(owner, assessment_id, generation=case["generation"])
            raise

    async def cancel(self, owner, assessment_id):
        self.service.state.stop(owner, assessment_id, cancel=True)
        if self.running and not self.running.done():
            self.running.cancel()
            await asyncio.gather(self.running, return_exceptions=True)
        return self.service.state.get(owner, assessment_id)


@pytest.mark.asyncio
async def test_http_saved_results_sources_and_export_never_start_research(setup_case):
    service, owner, case = setup_case
    original = b"Fictional source used for HTTP retrieval checks."
    source = service.evidence.save_source(
        owner,
        case["id"],
        kind="web",
        origin="https://example.com/fictional",
        original=original,
        text=original.decode(),
        media_type="text/plain",
        provenance={"provider": "simulation"},
        limitations=["No real research."],
    )
    dossier = publish_spinoff(service, owner, case, source.id)
    coordinator = GateCoordinator(service)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40, "other-owner": "b" * 40})
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://condere.test", headers=HEADERS
        ) as client,
    ):
        assert (
            await client.get(PREFIX + "/capabilities", headers={"Authorization": ""})
        ).status_code == 401
        capabilities = (await client.get(PREFIX + "/capabilities")).json()
        assert capabilities["agent_id"] == "condere"
        assert capabilities["result_schemas"] == ["condere.spinoff.v1"]
        listed = (await client.get(PREFIX)).json()
        assert case["id"] in {item["id"] for item in listed["assessments"]}
        assert (await client.get(PREFIX, headers=OTHER)).json()["assessments"] == []
        before_objects = service.evidence.client.calls
        for suffix in (
            "",
            "/report",
            "/sources",
            "/export",
            f"/sources/{source.id}/original",
            "/artifacts/artifacts/claims.json",
        ):
            denied = await client.get(PREFIX + "/" + case["id"] + suffix, headers=OTHER)
            assert denied.status_code == 404
        assert service.evidence.client.calls == before_objects
        report = await client.get(f"{PREFIX}/{case['id']}/report")
        assert report.json() == dossier.model_dump(mode="json")
        markdown = await client.get(f"{PREFIX}/{case['id']}/report?format=markdown")
        assert markdown.text == render_readme(dossier)
        claims = (
            await client.get(f"{PREFIX}/{case['id']}/artifacts/artifacts/claims.json?limit=50")
        ).json()
        assert claims["sha256"] == dossier.claims.full.sha256
        assert claims["next_offset"] == 50
        missing = await client.get(f"{PREFIX}/{case['id']}/artifacts/artifacts/none.md")
        assert missing.status_code == 404
        sources = (await client.get(f"{PREFIX}/{case['id']}/sources")).json()
        assert sources["sources"][0]["id"] == source.id
        window = (await client.get(f"{PREFIX}/{case['id']}/sources/{source.id}?limit=10")).json()
        assert window["citation"]["quote"] == original[:10].decode()
        assert window["next_offset"] == 10
        downloaded = await client.get(f"{PREFIX}/{case['id']}/sources/{source.id}/original")
        assert downloaded.content == original
        assert downloaded.headers["etag"] == '"' + source.original_sha256 + '"'
        archive = await client.get(f"{PREFIX}/{case['id']}/export")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
            assert original in [package.read(name) for name in package.namelist()]
            assert package.read("README.md").decode() == render_readme(dossier)
            for link in dossier.artifacts:
                assert hashlib.sha256(package.read(link.ref)).hexdigest() == link.sha256
        assert (await client.post(f"{PREFIX}/{case['id']}/continue")).status_code == 202
    assert coordinator.calls == []
    assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_http_admission_reconnect_duplicate_cancel_and_explicit_continue(setup_case):
    (
        service,
        owner,
        _,
    ) = setup_case
    campaign = str(uuid4())
    service.state.authorize_campaign(
        owner, campaign, 10000, {"basis": "HTTP fixture; no providers"}
    )
    coordinator = GateCoordinator(service)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40})
    body = {
        "campaign_id": campaign,
        "request_key": "same-request",
        "brief": {"original_input": "Fictional HTTP case."},
    }
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://condere.test", headers=HEADERS
        ) as client:
            assert (
                await client.post(PREFIX, json=body | {"cap_microusd": 99999999})
            ).status_code == 422
            created = await client.post(PREFIX, json=body)
            assert created.status_code == 202
            assessment_id = created.json()["id"]
            await asyncio.wait_for(coordinator.started.wait(), timeout=5)
            repeated = await client.post(PREFIX, json=body)
            assert repeated.json()["id"] == assessment_id
            changed = await client.post(
                PREFIX, json=body | {"brief": {"original_input": "Changed input"}}
            )
            assert changed.status_code == 409
        # Closing an HTTP client must not cancel admitted background work.
        assert not coordinator.running.done()
        async with httpx.AsyncClient(
            transport=transport, base_url="http://condere.test", headers=HEADERS
        ) as client:
            status = (await client.get(f"{PREFIX}/{assessment_id}")).json()
            assert status["status"] == "running"
            assert len(coordinator.calls) == 1
            cancelled = await client.post(f"{PREFIX}/{assessment_id}/cancel")
            assert cancelled.json()["status"] == "cancelled"
            coordinator.started.clear()
            continued = await client.post(f"{PREFIX}/{assessment_id}/continue")
            assert continued.status_code == 202
            await asyncio.wait_for(coordinator.started.wait(), timeout=5)
            assert coordinator.calls == [
                (owner, assessment_id, False),
                (owner, assessment_id, True),
            ]
            assert service.state.get(owner, assessment_id)["budget"]["cap_microusd"] == 10000
            await client.post(f"{PREFIX}/{assessment_id}/cancel")


@pytest.mark.asyncio
async def test_http_continuation_reports_uncertain_charges_without_releasing_them(setup_case):
    service, owner, case = setup_case
    operation = service.state.reserve_operation(
        owner, case["id"], case["generation"], "uncertain-request", "simulation", {}, 7000
    )
    service.state.dispatch(
        owner, case["id"], case["generation"], operation["id"], {"provider": "fixture"}
    )
    service.state.stop(owner, case["id"], generation=case["generation"])
    coordinator = GateCoordinator(service)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://condere.test", headers=HEADERS
        ) as client,
    ):
        assert (await client.post(f"{PREFIX}/{case['id']}/continue")).status_code == 202
        await asyncio.wait_for(coordinator.started.wait(), timeout=5)
        await asyncio.gather(coordinator.running)
        status = (await client.get(f"{PREFIX}/{case['id']}")).json()
        assert status["status"] == "interrupted"
        assert status["generation"] == case["generation"]
        assert status["spending"]["budget"]["pending_microusd"] == 7000
        assert status["execution_errors"][0]["payload"]["error_types"] == ["RecoveryRequired"]
    assert len(service.state.operations(owner, case["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CONDERE_TEST_SANDBOX") != "1", reason="Native VM opt-in required"
)
async def test_http_start_to_native_result_uses_the_real_coordinator(native_case, monkeypatch):
    service, owner, case, make, _, calls, *_ = native_case
    coordinator = make()
    completed = asyncio.Event()
    run = coordinator.run

    async def observed_run(*args, **kwargs):
        result = await run(*args, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(coordinator, "run", observed_run)
    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://condere.test", headers=HEADERS
        ) as client,
    ):
        response = await client.post(
            PREFIX,
            json={
                "campaign_id": case["campaign_id"],
                "request_key": case["request_key"],
                "brief": case["brief"],
            },
        )
        assert response.status_code == 202
        assert response.json()["id"] == case["id"]
        await asyncio.wait_for(completed.wait(), timeout=600)
        status = (await client.get(f"{PREFIX}/{case['id']}")).json()
        assert status["status"] == "completed"
        assert {item["status"] for item in status["workers"]} == {"completed"}
        report = (await client.get(f"{PREFIX}/{case['id']}/report")).json()
        assert report == service.report(owner, case["id"]).model_dump(mode="json")
        assert report["schema_version"] == "condere.spinoff.v1"
        assert report["build_plan"]["option"] == report["decision"]["option"]["value"]
        assert sorted(role for role, _ in calls) == ["code-analyst", "extractor", "lead", "planner"]
        assert service.state.operations(owner, case["id"]) == []


@pytest.mark.asyncio
async def test_http_hands_runs_to_a_launcher_instead_of_running_them(setup_case):
    service, owner, _ = setup_case
    campaign = str(uuid4())
    service.state.authorize_campaign(owner, campaign, 10000, {"basis": "HTTP fixture"})
    coordinator, launched = GateCoordinator(service), []

    async def launcher(owner_id, assessment_id, resume):
        launched.append((owner_id, assessment_id, resume))

    app = create_app(service, coordinator, owner_tokens={owner: "a" * 40}, launcher=launcher)
    body = {
        "campaign_id": campaign,
        "request_key": "cloud-job",
        "brief": {"original_input": "Fictional cloud case."},
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://condere.test", headers=HEADERS
        ) as client,
    ):
        created = await client.post(PREFIX, json=body)
        assert created.status_code == 202
        assessment_id = created.json()["id"]
        # One job per assessment: the API process itself runs nothing.
        assert launched == [(owner, assessment_id, False)]
        assert coordinator.calls == []
        service.state.stop(owner, assessment_id, cancel=True)
        assert (await client.post(f"{PREFIX}/{assessment_id}/continue")).status_code == 202
        assert launched[-1] == (owner, assessment_id, True)
