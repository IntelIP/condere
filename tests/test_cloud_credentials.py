"""The cloud deployment's ChatGPT plan session in Secret Manager, all traffic simulated."""

import base64
import contextlib
import json

import httpx
import pytest

from condere.infra.secret_manager import SecretRecord
from condere.runtime.chatgpt_plan import PLAN_SCOPE, CloudPlanCredentials, SignInRequired

SECRET = "projects/example-project/secrets/condere-agent-chatgpt"
HOST = "urn:uuid:00000000-0000-4000-8000-000000000001"


class SecretManager:
    """Secret Manager versions in memory."""

    def __init__(self):
        self.versions, self.destroyed = [], []

    def handle(self, request):
        if request.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer synthetic"
        path = request.url.path.removeprefix("/v1/")
        if path == f"{SECRET}/versions/latest:access":
            enabled = [item for item in self.versions if item["state"] == "ENABLED"]
            if not enabled:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"payload": {"data": enabled[-1]["data"]}})
        if path == f"{SECRET}:addVersion":
            name = f"{SECRET}/versions/{len(self.versions) + 1}"
            data = json.loads(request.content)["payload"]["data"]
            self.versions.append({"name": name, "data": data, "state": "ENABLED"})
            return httpx.Response(200, json={"name": name})
        if path == f"{SECRET}/versions":
            assert request.url.params["filter"] == "state:ENABLED"
            enabled = [item for item in self.versions if item["state"] == "ENABLED"]
            return httpx.Response(200, json={"versions": [{"name": v["name"]} for v in enabled]})
        if path.endswith(":destroy"):
            name = path.removesuffix(":destroy")
            next(item for item in self.versions if item["name"] == name)["state"] = "DESTROYED"
            self.destroyed.append(name)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    def latest(self):
        enabled = [item for item in self.versions if item["state"] == "ENABLED"]
        return json.loads(base64.b64decode(enabled[-1]["data"]))


def test_secret_record_keeps_only_the_latest_version_readable():
    manager = SecretManager()
    store = SecretRecord(SECRET, transport=httpx.MockTransport(manager.handle))
    assert store.load() is None
    store.save({"refresh_token": "refresh-1"})
    store.save({"refresh_token": "refresh-2"})
    assert store.load() == {"refresh_token": "refresh-2"}
    assert manager.destroyed == [f"{SECRET}/versions/1"]  # a spent refresh token is not kept
    with pytest.raises(ValueError, match="projects/"):
        SecretRecord("condere-agent-chatgpt")


def test_cloud_session_caches_access_and_serializes_rotating_refreshes():
    manager = SecretManager()
    store = SecretRecord(SECRET, transport=httpx.MockTransport(manager.handle))
    refreshes, locks = [], []

    def token_endpoint(request):
        form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        refreshes.append(form["refresh_token"])
        return httpx.Response(
            200,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )

    @contextlib.contextmanager
    def shared_lock():
        locks.append("held")
        yield

    credentials = CloudPlanCredentials(
        store, shared_lock, http=httpx.Client(transport=httpx.MockTransport(token_endpoint))
    )
    with pytest.raises(SignInRequired, match="chatgpt_cloud_signin"):
        credentials.host_id()  # not yet signed in
    store.save(
        {
            "client_id": "issued-client",
            "ext_agent_host_id": HOST,
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_at": 10_000,
            "scopes": [PLAN_SCOPE],
        }
    )
    assert credentials.host_id() == HOST
    assert credentials.access_token(now=lambda: 1_000) == "access-1"
    assert credentials.access_token(now=lambda: 2_000) == "access-1"
    assert locks == ["held"]  # the second call used the in-memory token
    assert credentials.access_token(now=lambda: 9_900) == "access-2"
    assert refreshes == ["refresh-1"] and len(locks) == 2
    # The rotated session is what every other process now reads.
    assert manager.latest()["refresh_token"] == "refresh-2"
    assert manager.latest()["ext_agent_host_id"] == HOST
