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

MODEL_ID = "openai/gpt-6.1-sol"
MODEL_CONTEXT_TOKENS = 1_050_000
MODEL_OUTPUT_TOKENS = 8_192
# Conservative long-context bounds, including the published cache-write premium.
# OpenRouter's model/endpoint catalogs checked 2026-10-22; these are not invoices.
PROMPT_USD_PER_TOKEN = Decimal("0.000005")
OUTPUT_USD_PER_TOKEN = Decimal("0.000015")
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

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if name in kwargs and (
                type(kwargs[name]) is not int or not 1 <= kwargs[name] <= MODEL_OUTPUT_TOKENS
            ):
                raise ValueError("The model request must preserve its admitted output-token cap.")
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        cap = payload.get("max_output_tokens")
        if type(cap) is not int or cap < 1 or cap > MODEL_OUTPUT_TOKENS:
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
            or payload["model"] != MODEL_ID
            or payload.get("stream") is not False
            or payload.get("store") is not False
            or payload.get("include", []) != ["reasoning.encrypted_content"]
        ):
            raise ValueError("The model request includes an unbounded or unapproved option.")
        if payload.get("extra_body") != router_options():
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
        maximum = microusd(
            MODEL_CONTEXT_TOKENS * PROMPT_USD_PER_TOKEN
            + payload["max_output_tokens"] * OUTPUT_USD_PER_TOKEN
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
            result.llm_output = {"token_usage": usage, "model_name": MODEL_ID}
            cost = usage.get("cost")
            known = cost is not None and not usage.get("is_byok", False)
            basis = (
                {
                    "basis": "OpenRouter reported usage cost; not an invoice",
                    "reported_usd": str(cost),
                    "rounding": "ceiling to integer microdollars",
                    "model": MODEL_ID,
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
                "model": MODEL_ID,
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


def router_options() -> dict:
    return {
        "provider": {
            "order": ["openai"],
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 4, "completion": 15, "image": 0, "request": 0},
        },
        "reasoning": {"effort": "medium"},
        "plugins": [],
    }


def accounted_model(
    context: ProviderContext, api_key: str, *, http_client=None
) -> AccountedChatOpenAI:
    if not api_key:
        raise ValueError("A scoped OpenRouter key is required.")
    model = AccountedChatOpenAI(
        model=MODEL_ID,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=MODEL_OUTPUT_TOKENS,
        max_retries=0,
        timeout=300,
        disable_streaming=True,
        stream_usage=False,
        use_responses_api=True,
        store=False,
        include=["reasoning.encrypted_content"],
        extra_body=router_options(),
        profile={
            "max_input_tokens": 48_000,
            "max_output_tokens": MODEL_OUTPUT_TOKENS,
            "tool_calling": True,
            "structured_output": True,
            "image_inputs": True,
        },
        http_client=http_client,
    )
    model._context = context
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
