"""Bounded OpenRouter and Exa adapters. Every paid dispatch uses the durable ledger.

Published prices are request bounds, not proof of billing. Exa explicitly labels
its returned costs as estimates, so those remain pending until reconciliation.
"""

import asyncio
import base64
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import message_to_dict, messages_from_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api
from pydantic import PrivateAttr

from condere.runtime.evidence import EvidenceStore
from condere.runtime.operations import ProviderResult, execute_once
from condere.runtime.state import AssessmentStore, Conflict, content_hash


@dataclass(frozen=True)
class ModelSpec:
    """One OpenRouter model with the bounds that support its maximum reservation."""

    id: str
    provider: str
    context_tokens: int
    output_tokens: int
    # Conservative per-token bounds, including any published cache-write premium.
    prompt_usd_per_token: Decimal
    output_usd_per_token: Decimal
    # OpenRouter max_price, in USD per million tokens, so routing cannot exceed the bound.
    max_price: dict


# OpenRouter's model/endpoint catalogs checked 2026-10-22 (GPT) and 2026-10-30 (Claude);
# these are request bounds, not invoices. Claude was verified live through the Responses
# API with tool calls, reasoning effort and encrypted reasoning on 2026-10-30.
GPT_SOL = ModelSpec(
    "openai/gpt-6.1-sol",
    "openai",
    1_050_000,
    8_192,
    Decimal("0.000005"),
    Decimal("0.000015"),
    {"prompt": 4, "completion": 15},
)
CLAUDE_OPUS = ModelSpec(
    "anthropic/claude-opus-5.5",
    "anthropic",
    1_000_000,
    16_384,
    Decimal("0.000005"),
    Decimal("0.00002"),
    {"prompt": 4, "completion": 20},
)
CLAUDE_HAIKU = ModelSpec(
    "anthropic/claude-haiku-5.5",
    "anthropic",
    1_000_000,
    8_192,
    Decimal("0.000000125"),
    Decimal("0.0000005"),
    {"prompt": 0.1, "completion": 0.5},
)
MODEL_ID = GPT_SOL.id
MODEL_CONTEXT_TOKENS = GPT_SOL.context_tokens
MODEL_OUTPUT_TOKENS = GPT_SOL.output_tokens
PROMPT_USD_PER_TOKEN = GPT_SOL.prompt_usd_per_token
OUTPUT_USD_PER_TOKEN = GPT_SOL.output_usd_per_token
SEARCH_BOUND_MICROUSD = 7_000  # auto search, <=10 results, text only; 2026-10-21 docs.
CONTENTS_BOUND_MICROUSD = 1_000  # one URL, text only; 2026-10-21 docs.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def microusd(value) -> int:
    if isinstance(value, bool):
        raise ValueError("A boolean is not a provider cost.")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Provider costs must be finite and nonnegative.")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class ProviderContext:
    state: AssessmentStore
    owner: str
    assessment_id: str
    generation: int
    worker_role: str
    question_id: str | None = None


def _encode_result(result: ChatResult) -> dict:
    return {
        "generations": [
            {"message": message_to_dict(item.message), "generation_info": item.generation_info}
            for item in result.generations
        ],
        "llm_output": result.llm_output,
    }


def _decode_result(value: dict) -> ChatResult:
    return ChatResult(
        generations=[
            ChatGeneration(
                message=messages_from_dict([item["message"]])[0],
                generation_info=item["generation_info"],
            )
            for item in value["generations"]
        ],
        llm_output=value["llm_output"],
    )


class AccountedChatOpenAI(ChatOpenAI):
    _context: ProviderContext = PrivateAttr()
    _spec: ModelSpec = PrivateAttr(default=GPT_SOL)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if name in kwargs and (
                type(kwargs[name]) is not int or not 1 <= kwargs[name] <= self._spec.output_tokens
            ):
                raise ValueError("The model request must preserve its admitted output-token cap.")
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        cap = payload.get("max_output_tokens")
        if type(cap) is not int or cap < 1 or cap > self._spec.output_tokens:
            raise ValueError("The model request must preserve its admitted output-token cap.")
        permitted = {
            "model",
            "input",
            "stream",
            "max_output_tokens",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "text",
            "store",
            "include",
            "extra_body",
        }
        if (
            set(payload) - permitted
            or payload["model"] != self._spec.id
            or payload.get("stream") is not False
            or payload.get("store") is not False
            or payload.get("include", []) != ["reasoning.encrypted_content"]
        ):
            raise ValueError("The model request includes an unbounded or unapproved option.")
        if payload.get("extra_body") != router_options(self._spec):
            raise ValueError(
                "Provider routing, pricing, and plugin restrictions cannot be overridden."
            )
        for tool in payload.get("tools", []):
            if tool.get("type") != "function":
                raise ValueError(
                    "Only Condere-owned function tools are allowed; native paid tools are disabled."
                )
        for message in payload["input"]:
            if message.get("type", "message") not in {
                "message",
                "function_call",
                "function_call_output",
                "reasoning",
            }:
                raise ValueError("Only local message and function-call history is allowed.")
            content = message.get("content", message.get("output"))
            if isinstance(content, list):
                for block in content:
                    if block.get("type") not in {"input_text", "output_text", "input_image"}:
                        raise ValueError(
                            "Convert files to text or bounded images before model dispatch."
                        )
                    if block.get("type") == "input_image" and not block.get(
                        "image_url", ""
                    ).startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
                        raise ValueError(
                            "Images must be supplied as admitted bytes, not external fetch URLs."
                        )
        return payload

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        context = self._context
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        key = "model:" + context.worker_role + ":" + content_hash(payload)
        spec = self._spec
        maximum = microusd(
            spec.context_tokens * spec.prompt_usd_per_token
            + payload["max_output_tokens"] * spec.output_usd_per_token
        )
        called = False

        def invoke():
            nonlocal called
            called = True
            response = self.root_client.responses.create(**payload)
            result = _construct_lc_result_from_responses_api(
                response, output_version="responses/v1"
            )
            # LangChain's Responses conversion drops provider-specific cost fields.
            # Preserve the raw SDK usage before conversion for the spending ledger.
            usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
            result.llm_output = {"token_usage": usage, "model_name": spec.id}
            cost = usage.get("cost")
            known = cost is not None and not usage.get("is_byok", False)
            basis = (
                {
                    "basis": "OpenRouter reported usage cost; not an invoice",
                    "reported_usd": str(cost),
                    "rounding": "ceiling to integer microdollars",
                    "model": spec.id,
                }
                if known
                else None
            )
            return ProviderResult(
                _encode_result(result),
                actual_microusd=microusd(cost) if known else None,
                cost_basis=basis,
            )

        operation = execute_once(
            context.state,
            owner=context.owner,
            assessment_id=context.assessment_id,
            generation=context.generation,
            key=key,
            kind="openrouter",
            request=payload,
            max_microusd=maximum,
            dispatch_identity={
                "provider": "openrouter",
                "model": spec.id,
                "local_request_key": key,
            },
            invoke=invoke,
        )
        if operation["state"] != "succeeded":
            raise Conflict(
                "The saved model operation failed; it will not be repeated automatically."
            )
        result = _decode_result(operation["result"])
        output = dict(result.llm_output or {})
        output["condere"] = {
            "operation_id": operation["id"],
            "reused": not called,
            "new_cost_microusd": operation["actual_microusd"] if called else 0,
        }
        if not called:
            output["condere"]["original_token_usage"] = output.get("token_usage")
            output["token_usage"] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost": 0,
            }
        result.llm_output = output
        for generation in result.generations:
            message = generation.message
            message.id = message.id or "condere:" + operation["id"]
            message.response_metadata["condere"] = output["condere"]
            if not called and hasattr(message, "usage_metadata"):
                message.usage_metadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        return result

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        manager = run_manager.get_sync() if run_manager is not None else None
        return await asyncio.to_thread(self._generate, messages, stop, manager, **kwargs)


def router_options(spec: ModelSpec = GPT_SOL) -> dict:
    return {
        "provider": {
            "order": [spec.provider],
            "only": [spec.provider],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": spec.max_price | {"image": 0, "request": 0},
        },
        "reasoning": {"effort": "medium"},
        "plugins": [],
    }


def accounted_model(
    context: ProviderContext, api_key: str, *, spec: ModelSpec = GPT_SOL, http_client=None
) -> AccountedChatOpenAI:
    if not api_key:
        raise ValueError("A scoped OpenRouter key is required.")
    model = AccountedChatOpenAI(
        model=spec.id,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=spec.output_tokens,
        max_retries=0,
        timeout=300,
        disable_streaming=True,
        stream_usage=False,
        use_responses_api=True,
        store=False,
        include=["reasoning.encrypted_content"],
        extra_body=router_options(spec),
        profile={
            "max_input_tokens": 48_000,
            "max_output_tokens": spec.output_tokens,
            "tool_calling": True,
            "structured_output": True,
            "image_inputs": True,
        },
        http_client=http_client,
    )
    model._context = context
    model._spec = spec
    return model


_SENSITIVE = re.compile(
    r"https?://|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\bsk-[A-Za-z0-9_-]{8,}|\b[0-9a-fA-F]{32,}\b"
    r"|(?:api[_ -]?key|authorization|bearer|password|token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def public_query(query: str, private_terms: tuple[str, ...] = ()) -> str:
    normalized = unicodedata.normalize("NFKC", query).strip()
    if not normalized or len(normalized) > 400 or any(ord(char) < 32 for char in normalized):
        raise ValueError("Use a short, single-line public-topic query.")
    if _SENSITIVE.search(normalized) or any(
        term and term.casefold() in normalized.casefold() for term in private_terms
    ):
        raise ValueError(
            "The query contains a private identifier, credential pattern, email, or URL."
        )
    without_dates = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", normalized)
    if re.search(r"\+?\d[\d ().-]{7,}\d", without_dates):
        raise ValueError("Contact details are not public research query terms.")
    return " ".join(normalized.split())


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("Use a public HTTP(S) source without credentials or custom ports.")
    host = parsed.hostname.lower()
    if (
        host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
    ):
        raise ValueError("Private destinations cannot be sent to public research providers.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Private addresses cannot be sent to public research providers.")
    if re.search(r"(?:token|key|password|signature|x-amz-|x-goog-)", parsed.query, re.IGNORECASE):
        raise ValueError("Signed or credential-bearing URLs are not public research inputs.")
    return value


class ExaResearch:
    def __init__(
        self,
        context: ProviderContext,
        evidence: EvidenceStore,
        api_key: str,
        *,
        private_terms: tuple[str, ...] = (),
        transport=None,
    ):
        if not api_key:
            raise ValueError("An Exa key is required.")
        self.context, self.evidence = context, evidence
        self.private_terms = (api_key, context.owner, context.assessment_id, *private_terms)
        self.client = httpx.Client(
            base_url="https://api.exa.ai",
            headers={"x-api-key": api_key},
            timeout=45,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def search(self, query: str, material_gap: str, *, num_results: int = 5) -> dict:
        if type(num_results) is not int or not 1 <= num_results <= 10:
            raise ValueError("Standard search permits 1-10 results within its fixed request bound.")
        query = public_query(query, self.private_terms)
        body = {
            "query": query,
            "type": "auto",
            "numResults": num_results,
            "contents": {"text": {"maxCharacters": 12_000}},
        }
        return self._request("/search", body, material_gap, SEARCH_BOUND_MICROUSD)

    def contents(self, source_id: str, material_gap: str) -> dict:
        source = self.evidence.source(self.context.owner, self.context.assessment_id, source_id)
        if source.kind != "web" or source.provenance.get("provider") != "exa":
            raise ValueError("Contents retrieval is limited to saved public Exa results.")
        url = public_url(source.origin)
        return self._request(
            "/contents",
            {"urls": [url], "text": {"maxCharacters": 12_000}},
            material_gap,
            CONTENTS_BOUND_MICROUSD,
        )

    def _request(self, path: str, body: dict, material_gap: str, maximum: int) -> dict:
        if not material_gap.strip():
            raise ValueError("Explain the material missing fact before purchasing new research.")
        context = self.context
        key = "exa:" + content_hash([path, body])

        def invoke():
            with self.client.stream("POST", path, json=body) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("The provider response exceeded its admitted size.")
                data = json.loads(raw)
                if not isinstance(data.get("results"), list):
                    raise ValueError("The provider did not return a recognizable result list.")
                return ProviderResult(
                    {
                        "data": data,
                        "original_base64": base64.b64encode(raw).decode(),
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "request_id": response.headers.get("x-request-id") or data.get("requestId"),
                    }
                )

        operation = execute_once(
            context.state,
            owner=context.owner,
            assessment_id=context.assessment_id,
            generation=context.generation,
            key=key,
            kind="exa",
            request={"path": path, "body": body},
            max_microusd=maximum,
            dispatch_identity={
                "provider": "exa",
                "local_request_key": key,
                "material_gap": material_gap,
            },
            invoke=invoke,
        )
        if operation["state"] != "succeeded":
            raise Conflict("The saved research operation failed; it will not be purchased again.")
        saved = operation["result"]
        data = saved["data"]
        estimate = data.get("costDollars", {}).get("total")
        bound = max(maximum, microusd(estimate)) if estimate is not None else maximum
        context.state.record_pending_cost(
            context.owner,
            context.assessment_id,
            operation["id"],
            bound,
            {
                "basis": (
                    "Exa published standard-request price bound; "
                    "response costDollars is an estimate, not an invoice"
                ),
                "response_estimate_usd": str(estimate) if estimate is not None else None,
                "request_id": saved["request_id"],
                "pricing_source": "https://docs.exa.ai/reference/pricing",
            },
        )
        source_ids = []
        rejected = []
        for index, item in enumerate(data["results"]):
            try:
                url = public_url(item["url"])
            except (ValueError, KeyError):
                rejected.append({"index": index, "reason": "Not an admissible public URL."})
                continue
            text = (
                item.get("text")
                or "Title: " + str(item.get("title", "Unavailable")) + "\nURL: " + url
            )
            limitations = ["Original bytes are the Exa response, not a publisher HTML archive."]
            if not item.get("text"):
                limitations.append("No page text was returned; only source metadata is available.")
            source = self.evidence.save_source(
                context.owner,
                context.assessment_id,
                kind="web",
                origin=url,
                original=base64.b64decode(saved["original_base64"]),
                text=text,
                media_type="application/json",
                provenance={
                    "provider": "exa",
                    "operation_id": operation["id"],
                    "request_id": saved["request_id"],
                    "result_index": index,
                    "published_at": item.get("publishedDate"),
                    "public": True,
                },
                limitations=limitations,
                retrieved_at=datetime.fromisoformat(saved["retrieved_at"]),
            )
            source_ids.append(source.id)
        return {
            "operation_id": operation["id"],
            "source_ids": source_ids,
            "rejected_results": rejected,
            "provider_notices": data.get("notices", []),
            "cost_status": "pending",
            "provider_estimate_usd": str(estimate) if estimate is not None else None,
            "reserved_microusd": bound,
        }


JEV_MODEL = "typesafe/jev-1.14"
JEV_PROMPT_USD_PER_TOKEN = Decimal("0.000000042")  # output is free; OpenRouter 2026-10-30.
# Jev's long-context mode (slower, less accurate) starts near 32,000 bytes of text.
JEV_STATE_BYTES = 32_000
JEV_REQUEST_BYTES = 64_000
JEV_QUESTIONS = 32
# Jev bills its own prompt overhead: live requests of a few hundred bytes billed 284
# tokens for one question and 441 for three (2026-10-30). Bound it generously.
JEV_OVERHEAD_TOKENS = 1_000
JEV_TOKENS_PER_QUESTION = 200


class JevDecisions:
    """Typed decisions from Jev through OpenRouter's System One endpoint.

    Jev returns probabilities, never prose. Callers combine answers in code.
    """

    def __init__(self, context: ProviderContext, api_key: str, *, transport=None):
        if not api_key:
            raise ValueError("A scoped OpenRouter key is required.")
        self.context = context
        self.client = httpx.Client(
            base_url="https://openrouter.ai/api/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def ask(self, state: list[str], questions: dict, purpose: str) -> dict:
        if not purpose.strip():
            raise ValueError("Name the decision these questions serve.")
        if not state or any(not isinstance(item, str) or not item.strip() for item in state):
            raise ValueError("Jev state must be nonempty text.")
        if len(questions) == 0 or len(questions) > JEV_QUESTIONS:
            raise ValueError(f"Ask between 1 and {JEV_QUESTIONS} questions per request.")
        if sum(len(item.encode()) for item in state) > JEV_STATE_BYTES:
            raise ValueError("The decision state must stay under 32,000 bytes.")
        for name, question in questions.items():
            if question.get("type") not in {"choice", "score", "noul"}:
                raise ValueError(f"Question {name} must be a choice, score or noul question.")
        body = {"model": JEV_MODEL, "state": state, "questions": questions}
        size = len(json.dumps(body).encode())
        if size > JEV_REQUEST_BYTES:
            raise ValueError("The decision request exceeds its admitted size.")
        context = self.context
        key = "jev:" + content_hash(body)
        # A token is at least one byte, so the request size bounds the supplied tokens;
        # Jev's own prompt overhead is added on top.
        tokens = size + JEV_OVERHEAD_TOKENS + JEV_TOKENS_PER_QUESTION * len(questions)
        maximum = microusd(tokens * JEV_PROMPT_USD_PER_TOKEN)

        def invoke():
            response = self.client.post("/systemone", json=body)
            response.raise_for_status()
            data = response.json()
            answers = data.get("answers")
            if not isinstance(answers, dict) or set(answers) != set(questions):
                raise ValueError("Jev did not answer exactly the questions asked.")
            cost = (data.get("usage") or {}).get("cost")
            return ProviderResult(
                {"model": data.get("model"), "answers": answers, "usage": data.get("usage")},
                actual_microusd=microusd(cost) if cost is not None else None,
                cost_basis=(
                    {
                        "basis": "OpenRouter reported usage cost; not an invoice",
                        "reported_usd": str(cost),
                        "model": data.get("model"),
                    }
                    if cost is not None
                    else None
                ),
            )

        operation = execute_once(
            context.state,
            owner=context.owner,
            assessment_id=context.assessment_id,
            generation=context.generation,
            key=key,
            kind="jev",
            request=body,
            max_microusd=maximum,
            dispatch_identity={"provider": "openrouter", "model": JEV_MODEL, "purpose": purpose},
            invoke=invoke,
        )
        if operation["state"] != "succeeded":
            raise Conflict("The saved decision operation failed; it will not be repeated.")
        return operation["result"] | {"operation_id": operation["id"]}
