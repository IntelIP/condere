"""Sign in with ChatGPT and plan-usage request shaping, with all OpenAI traffic simulated."""

import base64
import hashlib
import json
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from condere.runtime.chatgpt_plan import (
    PLAN_CODE_MODEL,
    PLAN_MODEL,
    PLAN_SCOPE,
    PlanCredentials,
    PlanUsageStopped,
    SignInRequired,
    authorization_request,
    complete_sign_in,
    plan_model,
)
from condere.runtime.providers import ProviderContext

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUED = "oaiapp_fixture"
GRANTED = "openid profile email offline_access resource.invoke " + PLAN_SCOPE


def id_token(nonce, *, sub="user-1", audience=ISSUED):
    return jwt.encode(
        {
            "iss": "https://auth.openai.com",
            "aud": audience,
            "sub": sub,
            "email": "founder@example.com",
            "nonce": nonce,
            "exp": 4_000_000_000,
            "iat": 1_000,
        },
        KEY,
        algorithm="RS256",
    )


@pytest.fixture
def home(tmp_path):
    return tmp_path / "chatgpt"


def token_endpoint(requests, *, scope=GRANTED, nonce_box=None, sub="user-1"):
    def handle(request):
        form = parse_qs(request.content.decode())
        requests.append({key: values[0] for key, values in form.items()})
        if form["grant_type"][0] == "refresh_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                    "scope": scope,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": id_token(nonce_box["nonce"], sub=sub),
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scope,
            },
        )

    return handle


def credentials_at(home, handler):
    credentials = PlanCredentials(home, http=httpx.Client(transport=httpx.MockTransport(handler)))
    return credentials


def test_first_registration_request_follows_the_dynamic_client_contract(home):
    credentials = PlanCredentials(home)
    url, attempt = authorization_request(credentials, "http://127.0.0.1:1455/auth/callback")
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert query["client_id"] == "dynamic_agent_client"
    assert query["agent_name_hint"] == "Condere"
    assert query["ext_agent_host_id"].startswith("urn:uuid:")
    assert query["ext_agent_host_id"] == credentials.host_id()  # Stable for this host.
    assert set(query["scope"].split()) == set(GRANTED.split())
    assert query["resource"] == "https://api.openai.com/v1"
    assert query["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    digest = hashlib.sha256(attempt["verifier"].encode()).digest()
    assert query["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert query["code_challenge_method"] == "S256" and "id_token_hint" not in query
    assert stat.S_IMODE((home / "host.json").stat().st_mode) == 0o600


def test_sign_in_validates_the_id_token_and_saves_private_credentials(home):
    requests, box = [], {}
    credentials = credentials_at(home, token_endpoint(requests, nonce_box=box))
    _, attempt = authorization_request(credentials, "http://127.0.0.1:1455/auth/callback")
    box["nonce"] = attempt["nonce"]
    with pytest.raises(SignInRequired, match="state"):
        complete_sign_in(credentials, attempt, {"state": "forged", "code": "c"})
    callback = {"state": attempt["state"], "code": "code-1", "client_id": ISSUED}
    record = complete_sign_in(
        credentials, attempt, callback, now=lambda: 1_000, signing_key=lambda _: KEY.public_key()
    )
    exchange = requests[-1]
    assert exchange["client_id"] == ISSUED and exchange["code_verifier"] == attempt["verifier"]
    assert exchange["resource"] == "https://api.openai.com/v1" and "client_secret" not in exchange
    assert record["client_id"] == ISSUED and record["subject"] == "user-1"
    assert record["expires_at"] == 4_600 and PLAN_SCOPE in record["scopes"]
    assert stat.S_IMODE(credentials.record_path.stat().st_mode) == 0o600
    assert credentials.status()["plan_usage"] is True
    # Reauthorization reuses the issued client and identifies the account without registering.
    url, again = authorization_request(credentials, "http://127.0.0.1:50000/auth/callback")
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert query["client_id"] == ISSUED and "agent_name_hint" not in query
    assert query["id_token_hint"] == record["id_token"]
    box["nonce"] = again["nonce"]
    other = credentials_at(home, token_endpoint([], nonce_box=box, sub="someone-else"))
    with pytest.raises(SignInRequired, match="different ChatGPT account"):
        complete_sign_in(
            other,
            again,
            {"state": again["state"], "code": "code-2"},
            signing_key=lambda _: KEY.public_key(),
        )


def test_declined_plan_usage_keeps_identity_but_blocks_inference(home):
    box = {}
    credentials = credentials_at(
        home, token_endpoint([], nonce_box=box, scope="openid profile email offline_access")
    )
    _, attempt = authorization_request(credentials, "http://127.0.0.1:1455/auth/callback")
    box["nonce"] = attempt["nonce"]
    with pytest.raises(SignInRequired, match="not granted"):
        complete_sign_in(
            credentials,
            attempt,
            {"state": attempt["state"], "code": "c", "client_id": ISSUED},
            signing_key=lambda _: KEY.public_key(),
        )
    assert credentials.load()["client_id"] == ISSUED
    with pytest.raises(SignInRequired, match="not granted"):
        credentials.access_token()
    with pytest.raises(SignInRequired, match="not enabled"):
        complete_sign_in(
            credentials, attempt, {"state": attempt["state"], "error": "access_denied"}
        )


def saved(home, handler, *, expires_at):
    credentials = credentials_at(home, handler)
    credentials.save(
        {
            "client_id": ISSUED,
            "subject": "user-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_at": expires_at,
            "scopes": GRANTED.split(),
        }
    )
    return credentials


def test_refresh_rotates_tokens_and_unusable_refresh_requires_sign_in(home):
    requests = []
    credentials = saved(home, token_endpoint(requests), expires_at=10_000)
    assert credentials.access_token(now=lambda: 1_000) == "access-1"
    assert requests == []
    assert credentials.access_token(now=lambda: 9_900) == "access-2"
    assert requests[-1]["client_id"] == ISSUED and requests[-1]["refresh_token"] == "refresh-1"
    assert credentials.load()["refresh_token"] == "refresh-2"

    def reused(request):
        return httpx.Response(400, json={"error": "refresh_token_reused"})

    credentials = saved(home, reused, expires_at=0)
    with pytest.raises(SignInRequired, match="refresh_token_reused"):
        credentials.access_token(now=lambda: 1_000)
    assert credentials.load()["refresh_token"] is None

    def unavailable(request):
        return httpx.Response(503, json={"detail": "try later"})

    credentials = saved(home, unavailable, expires_at=0)
    with pytest.raises(PlanUsageStopped):
        credentials.access_token(now=lambda: 1_000)
    assert credentials.load()["refresh_token"] == "refresh-1"  # Temporary failures keep tokens.


@tool
def read_assessment() -> dict:
    """Read the assessment."""
    return {}


def test_requests_follow_the_plan_route_contract(home):
    credentials = saved(home, token_endpoint([]), expires_at=4_000_000_000)
    context = ProviderContext(None, "owner", "case", 1, "lead")
    model = plan_model(context, credentials).bind_tools([read_assessment])
    payload = model.bound._get_request_payload(
        [SystemMessage("Condere instructions"), HumanMessage("Begin.")],
        **model.kwargs,
    )
    assert payload["model"] == PLAN_MODEL
    assert payload["store"] is False and payload["stream"] is True
    assert not {"max_output_tokens", "temperature", "metadata", "previous_response_id"} & set(
        payload
    )
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"effort": "medium"}
    [namespace] = payload["tools"]
    assert namespace["type"] == "namespace" and namespace["name"] == "condere"
    assert [item["name"] for item in namespace["tools"]] == ["read_assessment"]
    assert payload["input"][0]["role"] == "developer"
    assert json.dumps(payload).count("chatgpt-plan-token") == 0


def test_code_analysis_may_use_astra_and_no_other_model(home):
    credentials = saved(home, token_endpoint([]), expires_at=4_000_000_000)
    context = ProviderContext(None, "owner", "case", 1, "code-analyst")
    model = plan_model(context, credentials, model=PLAN_CODE_MODEL)
    payload = model._get_request_payload([HumanMessage("Read the code.")])
    assert payload["model"] == "gpt-6-astra"
    with pytest.raises(ValueError, match="not a selected plan-route model"):
        plan_model(context, credentials, model="gpt-4o")
    forced = model.bind_tools([read_assessment], tool_choice="required")
    payload = forced.bound._get_request_payload([HumanMessage("Read.")], **forced.kwargs)
    assert payload["tool_choice"] == "auto"  # Forcing made the model loop in reasoning.


def test_model_requires_sign_in(home):
    context = ProviderContext(None, "owner", "case", 1, "lead")
    with pytest.raises(SignInRequired):
        plan_model(context, PlanCredentials(home))
