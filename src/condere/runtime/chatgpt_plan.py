"""Use the operator's ChatGPT plan for model calls through Sign in with ChatGPT.

OpenAI documents ChatGPT plan usage for open-source and locally hosted apps
(https://developers.openai.com/siwc/token-sharing-open-source), including the owner's own
instance on a remote host (the "self-hosted VMs" guide): that host gets its own host ID, is
signed in locally, and then owns its refreshes (`CloudPlanCredentials`). Offering plan usage
to other people from a paid or remotely hosted app needs OpenAI partner access first.

Sign-in registers Condere dynamically (`dynamic_agent_client`) for one ChatGPT account and
workspace, bound to a persistent host ID. Credentials live in a private local directory, or
for the cloud deployment in Secret Manager; never in the repository, Doppler, logs, or
traces. The refresh token rotates, so refreshes are serialized (a file lock locally, the
database in the cloud). Inference uses the public Responses API with `store: false` and
`stream: true`; plan usage stops on limits rather than switching billing.
"""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api
from openai import APIError, APIStatusError, OpenAI
from pydantic import PrivateAttr

from condere.runtime.operations import ProviderRefused, ProviderResult, execute_once
from condere.runtime.providers import (
    WORKING_CONTEXT_TOKENS,
    ProviderContext,
    _decode_result,
    _encode_result,
)
from condere.runtime.state import StateError, content_hash

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
AGENT_NAME = "Condere"
USAGE_URL = "https://chatgpt.com/settings/usage"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
DEFAULT_HOME = Path.home() / ".local/share/condere-mda/chatgpt"
PLAN_MODEL = "gpt-6.1-sol"
# OpenAI's most capable model (reasoning and coding), the plan-route counterpart of Claude Opus
# for code analysis. Owner direction 2026-10-30: run code analysis on ChatGPT plan usage after
# two Opus code analyses cost $11.92 and $12.96 on OpenRouter. Both models answered on the
# plan route the same day.
PLAN_CODE_MODEL = "gpt-6-astra"
# Extraction moved to the plan too (owner, 2026-10-30), leaving Jev as the only per-call
# model charge. Terra is OpenAI's fast, efficient tier above Luna; quote fit and firsthand
# evidence are the weakest judged areas, so the stronger of the two starts.
PLAN_EXTRACT_MODEL = "gpt-6.1-terra"
PLAN_MODELS = {PLAN_MODEL, PLAN_CODE_MODEL, PLAN_EXTRACT_MODEL}
REFRESH_MARGIN_SECONDS = 300
# Responses fields this route rejects (preview limitations, checked 2026-10-28).
UNSUPPORTED_FIELDS = {
    "background",
    "conversation",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "moderation",
    "multi_agent",
    "previous_response_id",
    "prompt",
    "prompt_cache_retention",
    "safety_identifier",
    "temperature",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
}
# Refresh failures after which the stored tokens are unusable (errors-and-recovery docs).
UNUSABLE_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}
# Failures that stop work now but may succeed after the user's limits reset or service recovers.
RETRYABLE = {
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable",
    "subscription_sharing_user_unavailable",
    "admission_unavailable",
    "transport_error",
    "server_is_overloaded",
    "server_error",
}


TRANSIENT_BACKOFF_SECONDS = (5, 20, 60)
STREAM_IDLE_SECONDS = 120
# A stream can keep sending events without finishing: an extractor call ran 900 s before the
# route ended it (2026-10-30), holding the critical path for 15 minutes. Subagent calls
# normally take under a minute, lead and planner calls at most ~3 minutes.
STREAM_TOTAL_SECONDS = {"lead": 300, "planner": 300}
SUBAGENT_STREAM_TOTAL_SECONDS = 180


def retryable(code: str) -> bool:
    """Worth repeating later: limits, temporary service conditions, and any server error."""
    return code in RETRYABLE or code.startswith("http_5")


def transient(code: str) -> bool:
    """Retried inside the worker; a usage limit instead stops until the user's limit resets."""
    return retryable(code) and code != "subscription_sharing_usage_limit_exceeded"


class SignInRequired(StateError):
    """No usable ChatGPT credentials; run the Condere ChatGPT sign-in again."""


class PlanUsageStopped(StateError):
    """ChatGPT plan usage refused or interrupted a request; accepted work is preserved."""

    def __init__(self, code: str, detail: str):
        self.code = code
        guidance = (
            f" Review usage and this app's limit at {USAGE_URL}."
            if code == "subscription_sharing_usage_limit_exceeded"
            else ""
        )
        super().__init__(f"ChatGPT plan request stopped ({code}): {detail}.{guidance}")


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    temporary.replace(path)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class PlanCredentials:
    """One ChatGPT account registration and this host's identity, stored privately."""

    def __init__(self, home: Path = DEFAULT_HOME, *, http: httpx.Client | None = None):
        self.home = home
        self._http = http
        self._lock = threading.Lock()

    @property
    def record_path(self) -> Path:
        return self.home / "credentials.json"

    def client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)

    def host_id(self) -> str:
        """Stable, opaque host identifier chosen before the first sign-in and reused."""
        path = self.home / "host.json"
        if path.exists():
            return json.loads(path.read_text())["ext_agent_host_id"]
        value = "urn:uuid:" + str(uuid.uuid4())
        _write_private(path, {"ext_agent_host_id": value})
        return value

    def load(self) -> dict | None:
        return json.loads(self.record_path.read_text()) if self.record_path.exists() else None

    def save(self, record: dict) -> None:
        _write_private(self.record_path, record)

    @contextlib.contextmanager
    def _serialized(self):
        """Serialize refreshes across threads and processes; refresh tokens rotate."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock, (self.home / "refresh.lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self) -> dict:
        record = self.load()
        if not record or not record.get("access_token"):
            return {"signed_in": False, "plan_usage": False}
        return {
            "signed_in": True,
            "email": record.get("email"),
            "client_id": record["client_id"],
            "plan_usage": PLAN_SCOPE in record.get("scopes", []),
            "access_expires_at": record["expires_at"],
            "manage_usage": USAGE_URL,
        }

    def access_token(self, now: Callable[[], float] = time.time) -> str:
        with self._serialized():
            record = self.load()
            if not record or not record.get("refresh_token"):
                raise SignInRequired("Sign in with ChatGPT before using plan usage.")
            if PLAN_SCOPE not in record.get("scopes", []):
                raise SignInRequired("ChatGPT plan usage was not granted; sign in again.")
            if record["expires_at"] - now() > REFRESH_MARGIN_SECONDS:
                return record["access_token"]
            return self._refresh(record, now)["access_token"]

    def _refresh(self, record: dict, now: Callable[[], float]) -> dict:
        client = self.client()
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"],
                    "resource": RESOURCE,
                },
            )
        finally:
            if self._http is None:
                client.close()
        if response.status_code != 200:
            code = _error_code(response)
            if code == "invalid_client":
                raise SignInRequired("The ChatGPT client registration is invalid; sign in again.")
            if code in UNUSABLE_REFRESH:
                self.save({**record, "access_token": None, "refresh_token": None})
                raise SignInRequired(f"ChatGPT refresh was rejected ({code}); sign in again.")
            raise PlanUsageStopped(
                "admission_unavailable", f"token refresh HTTP {response.status_code} ({code})"
            )
        renewed = response.json()
        updated = record | {
            "access_token": renewed["access_token"],
            "refresh_token": renewed.get("refresh_token", record["refresh_token"]),
            "expires_at": now() + int(renewed.get("expires_in", 3600)),
            "scopes": renewed.get("scope", " ".join(record["scopes"])).split(),
            "saved_at": datetime.now(UTC).isoformat(),
        }
        self.save(updated)
        return updated

    def sign_out(self) -> bool:
        """Revoke the renewable session, clear tokens, and keep the client and host IDs."""
        with self._serialized():
            record = self.load()
            if not record or not record.get("refresh_token"):
                return True
            client = self.client()
            try:
                endpoint = client.get(DISCOVERY_URL).json()["revocation_endpoint"]
                revoked = (
                    client.post(
                        endpoint,
                        data={
                            "token": record["refresh_token"],
                            "token_type_hint": "refresh_token",
                            "client_id": record["client_id"],
                        },
                    ).status_code
                    == 200
                )
            except httpx.HTTPError:
                revoked = False
            finally:
                if self._http is None:
                    client.close()
            self.save({**record, "access_token": None, "refresh_token": None})
            return revoked


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}"
    if isinstance(body.get("error"), dict):
        return body["error"].get("code") or f"http_{response.status_code}"
    return body.get("error") or f"http_{response.status_code}"


class CloudPlanCredentials(PlanCredentials):
    """The cloud deployment's own ChatGPT plan session, kept in Secret Manager.

    OpenAI's self-hosted VM path: the deployment is its own agent host with its own
    `ext_agent_host_id`, signed in locally (scripts/chatgpt_cloud_signin.py) and from then on
    the only refresher of that session. The API and every assessment job share it: refreshes
    are serialized through the database because the refresh token rotates, and each process
    keeps the access token in memory until it nears expiry.
    """

    def __init__(self, store, lock: Callable, *, http: httpx.Client | None = None):
        super().__init__(Path("/nonexistent/condere-chatgpt"), http=http)
        self.store, self._shared_lock = store, lock
        self._record = None

    def host_id(self) -> str:
        record = self.store.load()
        if not record or not record.get("ext_agent_host_id"):
            raise SignInRequired("Sign the deployment in: scripts/chatgpt_cloud_signin.py")
        return record["ext_agent_host_id"]

    def load(self) -> dict | None:
        self._record = self.store.load()
        return self._record

    def save(self, record: dict) -> None:
        self.store.save(record)
        self._record = record

    @contextlib.contextmanager
    def _serialized(self):
        with self._lock, self._shared_lock():
            yield

    def access_token(self, now: Callable[[], float] = time.time) -> str:
        cached = self._record
        if (
            cached
            and cached.get("access_token")
            and PLAN_SCOPE in cached.get("scopes", [])
            and cached["expires_at"] - now() > REFRESH_MARGIN_SECONDS
        ):
            return cached["access_token"]
        return super().access_token(now)


def authorization_request(credentials: PlanCredentials, redirect_uri: str) -> tuple[str, dict]:
    """Build the authorize URL and the per-attempt secrets that must survive the callback."""
    record = credentials.load()
    attempt = {
        "state": _b64url(secrets.token_bytes(32)),
        "nonce": _b64url(secrets.token_bytes(32)),
        "verifier": _b64url(secrets.token_bytes(48)),
        "redirect_uri": redirect_uri,
        "client_id": record["client_id"] if record else "dynamic_agent_client",
    }
    params = {
        "client_id": attempt["client_id"],
        "ext_agent_host_id": credentials.host_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": attempt["state"],
        "nonce": attempt["nonce"],
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(attempt["verifier"].encode()).digest()),
    }
    if record is None:
        params["agent_name_hint"] = AGENT_NAME
    else:
        if record.get("id_token"):
            params["id_token_hint"] = record["id_token"]
        if record.get("email"):
            params["login_hint"] = record["email"]
    return AUTHORIZE_URL + "?" + urlencode(params), attempt


def complete_sign_in(
    credentials: PlanCredentials,
    attempt: dict,
    callback: dict[str, str],
    *,
    now: Callable[[], float] = time.time,
    signing_key: Callable[[str], object] | None = None,
) -> dict:
    """Validate the callback, exchange the code, verify the ID token, and save credentials."""
    if callback.get("state") != attempt["state"]:
        raise SignInRequired("The sign-in callback state did not match; start again.")
    if callback.get("error"):
        raise SignInRequired(
            "ChatGPT plan usage was not enabled."
            if callback["error"] == "access_denied"
            else f"Sign-in failed: {callback['error']}."
        )
    existing = credentials.load()
    client_id = attempt["client_id"]
    if client_id == "dynamic_agent_client":
        client_id = callback.get("client_id") or ""
        if not client_id or client_id == "dynamic_agent_client":
            raise SignInRequired("Registration did not return an issued client ID.")
    elif callback.get("client_id") not in {None, client_id}:
        raise SignInRequired("The callback named a different client; registration not replaced.")
    client = credentials.client()
    try:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": callback["code"],
                "code_verifier": attempt["verifier"],
                "redirect_uri": attempt["redirect_uri"],
                "resource": RESOURCE,
            },
        )
        if response.status_code != 200:
            raise SignInRequired(f"Code exchange failed ({_error_code(response)}); start again.")
        tokens = response.json()
        if signing_key is None:
            jwks = jwt.PyJWKClient(client.get(DISCOVERY_URL).json()["jwks_uri"])

            def signing_key(token):
                return jwks.get_signing_key_from_jwt(token).key

    finally:
        if credentials._http is None:
            client.close()
    claims = jwt.decode(
        tokens["id_token"],
        signing_key(tokens["id_token"]),
        algorithms=["RS256", "ES256"],
        audience=client_id,
        issuer=ISSUER,
    )
    if claims.get("nonce") != attempt["nonce"]:
        raise SignInRequired("The ID token nonce did not match this sign-in.")
    if existing and existing.get("subject") not in {None, claims["sub"]}:
        raise SignInRequired("A different ChatGPT account signed in; registration not replaced.")
    scopes = tokens.get("scope", callback.get("scope", "")).split()
    record = {
        "issuer": ISSUER,
        "subject": claims["sub"],
        "email": claims.get("email"),
        "client_id": client_id,
        "ext_agent_host_id": credentials.host_id(),
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_at": now() + int(tokens.get("expires_in", 3600)),
        "scopes": scopes,
        "saved_at": datetime.now(UTC).isoformat(),
        "welcomed": bool(existing and existing.get("welcomed")),
    }
    credentials.save(record)
    if PLAN_SCOPE not in scopes:
        raise SignInRequired(
            "Signed in, but ChatGPT plan usage was not granted. Sign in again and allow it, "
            "or use the OpenRouter provider."
        )
    return record


def sign_in(
    credentials: PlanCredentials,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    announce: Callable[[str], None] = print,
    timeout: float = 600,
) -> dict:
    """Run the loopback browser flow on 127.0.0.1 and save validated credentials."""
    received: dict[str, str] = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler name
            parsed = urlsplit(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            received.update({key: values[0] for key, values in parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Condere received the ChatGPT sign-in. You can close this tab.")
            done.set()

        def log_message(self, *args):  # Keep codes and tokens out of logs.
            return

    port = CALLBACK_PORT if credentials.load() is None else 0
    server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
        url, attempt = authorization_request(credentials, redirect)
        announce("Continue with ChatGPT: opening your browser to authorize Condere.")
        if "id_token_hint" not in url:  # An ID-token hint must stay out of terminals and logs.
            announce("If the browser does not open, visit this link on this computer:\n" + url)
        open_browser(url)
        if not done.wait(timeout):
            raise SignInRequired("Timed out waiting for ChatGPT authorization.")
    finally:
        server.shutdown()
        server.server_close()
    return complete_sign_in(credentials, attempt, received)


class PlanChatOpenAI(ChatOpenAI):
    """Responses API calls billed to the signed-in ChatGPT plan, recorded in the ledger."""

    _context: ProviderContext = PrivateAttr()
    _credentials: PlanCredentials = PrivateAttr()

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload = {key: value for key, value in payload.items() if key not in UNSUPPORTED_FIELDS}
        if payload.get("model") not in PLAN_MODELS:
            raise ValueError("The plan route uses only the selected Condere models.")
        payload |= {"store": False, "stream": True}
        # A forced tool call (`required`, sent for a subagent's structured output) made Terra
        # and Astra emit ~100 reasoning items and no output until the plan route ended the
        # response with `max_messages` (replayed 2026-10-30). The model still ends with the
        # structured-output tool when it is free to choose, as Claude did with `auto`.
        if payload.get("tool_choice") not in (None, "auto"):
            payload["tool_choice"] = "auto"
        functions = []
        for tool in payload.pop("tools", []):
            if tool.get("type") != "function":
                raise ValueError("Only Condere-owned function tools are allowed on this route.")
            functions.append(tool)
        if functions:
            payload["tools"] = [
                {
                    "type": "namespace",
                    "name": "condere",
                    "description": "Condere assessment, evidence, sandbox and submission tools.",
                    "tools": functions,
                }
            ]
        items = []
        for item in payload["input"]:
            kind = item.get("type", "message")
            if kind not in {"message", "function_call", "function_call_output", "reasoning"}:
                raise ValueError("Only local message and function-call history is allowed.")
            if kind == "message" and item.get("role") == "system":
                item = item | {"role": "developer"}  # System items are rejected on this route.
            content = item.get("content", item.get("output"))
            for block in content if isinstance(content, list) else []:
                if block.get("type") not in {"input_text", "output_text", "input_image"}:
                    raise ValueError("Convert files to text or bounded images before dispatch.")
            items.append(item)
        payload["input"] = items
        return payload

    def _attempt_key(self, payload: dict) -> str:
        """Reuse a saved answer; retry only after a retryable stop and explicit continuation."""
        context = self._context
        base = f"model:chatgpt:{context.worker_role}:{content_hash(payload)}"
        attempts = [
            item
            for item in context.state.operations(context.owner, context.assessment_id)
            if item["operation_key"] == base or item["operation_key"].startswith(base + ":retry-")
        ]
        if not attempts:
            return base
        latest = attempts[-1]
        if latest["state"] == "failed" and (latest["result"] or {}).get("retryable"):
            return f"{base}:retry-{len(attempts)}"
        return latest["operation_key"]

    def _stream(self, payload: dict):
        client = OpenAI(
            api_key=self._credentials.access_token(),
            base_url=RESOURCE,
            max_retries=0,
            # Calls stream events while they work; a stream silent for STREAM_IDLE_SECONDS is
            # treated as a transport error and retried. A lead call that hung silently for the
            # full 300 s on 2026-10-30 delayed all research by 6 minutes.
            timeout=httpx.Timeout(300, read=STREAM_IDLE_SECONDS, connect=15),
            http_client=self.http_client,
        )
        limit = STREAM_TOTAL_SECONDS.get(self._context.worker_role, SUBAGENT_STREAM_TOTAL_SECONDS)
        deadline = time.monotonic() + limit
        try:
            with client.responses.create(**payload) as stream:
                # The plan route can complete with an empty `output` and deliver the items only
                # as output_item.done events (a live lead call on 2026-10-30 lost its tool call).
                items = {}
                for event in stream:
                    if time.monotonic() > deadline:
                        return None, {
                            "code": "transport_error",
                            "detail": f"stream did not complete within {limit} s",
                        }
                    if event.type == "response.output_item.done":
                        items[event.output_index] = event.item
                    if event.type == "response.completed":
                        response = event.response
                        if not response.output and items:
                            response = response.model_copy(
                                update={"output": [items[index] for index in sorted(items)]}
                            )
                        return response, None
                    if event.type in {"response.failed", "response.incomplete"}:
                        error = getattr(event.response, "error", None)
                        details = getattr(event.response, "incomplete_details", None)
                        code = (
                            getattr(error, "code", None)
                            or getattr(details, "reason", None)
                            or event.type
                        )
                        return None, {"code": code, "detail": event.type}
            return None, {"code": "transport_error", "detail": "stream ended without completion"}
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            nested = body.get("error") if isinstance(body.get("error"), dict) else body
            code = (nested or {}).get("code") or (
                "admission_unavailable" if error.status_code == 503 else f"http_{error.status_code}"
            )
            return None, {
                "code": code,
                "detail": str((nested or {}).get("message") or body.get("detail") or ""),
                "status": error.status_code,
                "request_id": error.request_id,
            }
        except APIError as error:  # Mid-stream error events and connection failures.
            return None, {
                "code": "transport_error",
                "detail": f"{getattr(error, 'code', None) or type(error).__name__}: "
                + str(getattr(error, "message", error))[:300],
            }
        finally:
            if self.http_client is None:  # Never close a caller-supplied HTTP client.
                client.close()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Retry transient plan-route failures with bounded backoff (OpenAI's guidance).

        Each attempt is its own ledger operation. Usage limits and rejected requests stop
        immediately; plan usage carries no per-request dollar liability to reconcile.
        """
        for delay in (*TRANSIENT_BACKOFF_SECONDS, None):
            try:
                return self._generate_once(messages, stop, run_manager, **kwargs)
            except PlanUsageStopped as error:
                if not transient(error.code) or delay is None:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _generate_once(self, messages, stop=None, run_manager=None, **kwargs):
        context = self._context
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        key = self._attempt_key(payload)
        called = False

        def invoke():
            nonlocal called
            called = True
            try:
                response, failure = self._stream(payload)
            except (SignInRequired, PlanUsageStopped) as error:
                failure = {"code": getattr(error, "code", "sign_in_required"), "detail": str(error)}
                response = None
            except Exception as error:  # Interrupted transport: record, never retry here.
                response, failure = (
                    None,
                    {"code": "transport_error", "detail": type(error).__name__},
                )
            basis = {
                "basis": "ChatGPT plan usage; no per-request charge is reported",
                "limitations": [
                    "Plan limits apply; credits may be used after limits only if the user "
                    "allowed it in ChatGPT settings.",
                    "Manage usage at " + USAGE_URL,
                ],
                "model": self.model_name,
            }
            if failure is not None:
                return ProviderResult(
                    failure | {"retryable": retryable(failure["code"])},
                    succeeded=False,
                    actual_microusd=0,
                    cost_basis=basis,
                )
            result = _construct_lc_result_from_responses_api(
                response, output_version="responses/v1"
            )
            usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
            result.llm_output = {"token_usage": usage, "model_name": self.model_name}
            return ProviderResult(
                _encode_result(result), actual_microusd=0, cost_basis=basis | {"usage": usage}
            )

        operation = execute_once(
            context.state,
            owner=context.owner,
            assessment_id=context.assessment_id,
            generation=context.generation,
            key=key,
            kind="chatgpt_plan",
            request=payload,
            max_microusd=0,
            dispatch_identity={
                "provider": "chatgpt_plan",
                "model": self.model_name,
                "local_request_key": key,
            },
            invoke=invoke,
        )
        if operation["state"] != "succeeded":
            failure = operation["result"] or {}
            code, detail = failure.get("code", "unknown"), failure.get("detail", "")
            if not retryable(code):
                # A refusal of this request (e.g. `max_messages`, seen 2026-10-30 when a code
                # analyst reached 76 items on Astra) ends this call, not the assessment.
                raise ProviderRefused("chatgpt_plan", None, f"{code}: {detail}")
            raise PlanUsageStopped(code, detail)
        result = _decode_result(operation["result"])
        output = dict(result.llm_output or {})
        output["condere"] = {
            "operation_id": operation["id"],
            "reused": not called,
            "billing": "chatgpt_plan",
        }
        result.llm_output = output
        for generation in result.generations:
            generation.message.id = generation.message.id or "condere:" + operation["id"]
            generation.message.response_metadata["condere"] = output["condere"]
        return result

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        manager = run_manager.get_sync() if run_manager is not None else None
        return await asyncio.to_thread(self._generate, messages, stop, manager, **kwargs)


def plan_model(
    context: ProviderContext,
    credentials: PlanCredentials,
    *,
    model: str = PLAN_MODEL,
    http_client=None,
) -> PlanChatOpenAI:
    if credentials.load() is None:
        raise SignInRequired("Run the Condere ChatGPT sign-in before a live assessment.")
    if model not in PLAN_MODELS:
        raise ValueError(f"{model} is not a selected plan-route model.")
    chat = PlanChatOpenAI(
        model=model,
        api_key="chatgpt-plan-token-supplied-per-request",
        base_url=RESOURCE,
        max_retries=0,
        timeout=300,
        disable_streaming=True,
        use_responses_api=True,
        store=False,
        include=["reasoning.encrypted_content"],
        reasoning={"effort": "medium"},
        profile={
            "max_input_tokens": WORKING_CONTEXT_TOKENS,
            "tool_calling": True,
            "structured_output": True,
            "image_inputs": True,
        },
        http_client=http_client,
    )
    chat._context = context
    chat._credentials = credentials
    return chat
