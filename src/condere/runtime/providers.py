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
from datetime import UTC, date, datetime
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
    # OpenRouter's Anthropic endpoints accept only tool_choice "auto" on the Responses API
    # (live probe 2026-10-30: "required" and a named function return 404 "No endpoints").
    forced_tool_choice: bool = True


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
    forced_tool_choice=False,
)
CLAUDE_HAIKU = ModelSpec(
    "anthropic/claude-haiku-5.5",
    "anthropic",
    1_000_000,
    8_192,
    Decimal("0.000000125"),
    Decimal("0.0000005"),
    {"prompt": 0.1, "completion": 0.5},
    forced_tool_choice=False,
)
MODEL_ID = GPT_SOL.id
MODEL_CONTEXT_TOKENS = GPT_SOL.context_tokens
MODEL_OUTPUT_TOKENS = GPT_SOL.output_tokens
PROMPT_USD_PER_TOKEN = GPT_SOL.prompt_usd_per_token
OUTPUT_USD_PER_TOKEN = GPT_SOL.output_usd_per_token
# Exa published prices, checked 2026-10-30: base request covers 1-10 results; results
# 11-25 add $1/1k; each page's text or highlights is $1/1k pages; >25 needs Enterprise.
EXA_REQUEST_USD = {
    "auto": Decimal("0.007"),
    "deep-lite": Decimal("0.012"),
    "deep": Decimal("0.012"),
}
EXA_EXTRA_RESULT_USD = Decimal("0.001")
EXA_PAGE_CONTENT_USD = Decimal("0.001")
# Exa publishes no price for live fetching or additional deep queries; bound them generously.
EXA_LIVE_PAGE_MARGIN_USD = Decimal("0.002")
EXA_ADDITIONAL_QUERY_USD = Decimal("0.007")
EXA_MAX_RESULTS = 25
EXA_ADDITIONAL_QUERIES = 5
EXA_LINKS = 50
EXA_CATEGORIES = {"news", "company", "publication", "personal site", "financial report", "people"}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def exa_page_bound(*, fresh: bool) -> int:
    return microusd(EXA_PAGE_CONTENT_USD + (EXA_LIVE_PAGE_MARGIN_USD if fresh else 0))


def exa_bound(depth: str, results: int, fresh: bool, additional_queries: int) -> int:
    """A conservative reservation for one search; the response estimate can only raise it."""
    return microusd(
        EXA_REQUEST_USD[depth]
        + max(results - 10, 0) * EXA_EXTRA_RESULT_USD
        + additional_queries * EXA_ADDITIONAL_QUERY_USD
    ) + results * exa_page_bound(fresh=fresh)


_DOMAIN = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}(/[A-Za-z0-9._~/-]*)?$")


def public_domains(values) -> list[str]:
    domains = [value.strip().lower() for value in values]
    if len(domains) > 20 or any(not _DOMAIN.fullmatch(item) for item in domains):
        raise ValueError("Use at most 20 public hostnames, optionally with a path or *. prefix.")
    return domains


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
        if not self._spec.forced_tool_choice and payload.get("tool_choice") not in (None, "auto"):
            # Structured output still arrives as a tool call; the subagent prompt asks for it.
            payload["tool_choice"] = "auto"
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

    def search(
        self,
        query: str,
        material_gap: str,
        *,
        num_results: int = 10,
        depth: str = "auto",
        additional_queries: tuple[str, ...] = (),
        published_after: date | None = None,
        published_before: date | None = None,
        category: str | None = None,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
        fresh: bool = False,
        content: str = "highlights",
    ) -> dict:
        """Search public sources. Highlights are query-focused excerpts; use text sparingly."""
        if type(num_results) is not int or not 1 <= num_results <= EXA_MAX_RESULTS:
            raise ValueError(f"Search permits 1-{EXA_MAX_RESULTS} results.")
        if depth not in EXA_REQUEST_USD:
            raise ValueError(f"Search depth must be one of {sorted(EXA_REQUEST_USD)}.")
        if additional_queries and depth == "auto":
            raise ValueError("Additional queries require deep-lite or deep search.")
        if len(additional_queries) > EXA_ADDITIONAL_QUERIES:
            raise ValueError(f"Use at most {EXA_ADDITIONAL_QUERIES} additional queries.")
        if category is not None and category not in EXA_CATEGORIES:
            raise ValueError(f"Category must be one of {sorted(EXA_CATEGORIES)}.")
        dated = published_after is not None or published_before is not None
        if dated and category in {"company", "people"}:
            raise ValueError("Exa rejects publication dates for company and people searches.")
        if published_after and published_before and published_after > published_before:
            raise ValueError("The publication window must start before it ends.")
        if content not in {"highlights", "text"}:
            raise ValueError("Content must be highlights or text.")
        query = public_query(query, self.private_terms)
        body = {"query": query, "type": depth, "numResults": num_results}
        if additional_queries:
            body["additionalQueries"] = [
                public_query(item, self.private_terms) for item in additional_queries
            ]
        if category:
            body["category"] = category
        if published_after:
            body["startPublishedDate"] = f"{published_after.isoformat()}T00:00:00.000Z"
        if published_before:
            body["endPublishedDate"] = f"{published_before.isoformat()}T23:59:59.999Z"
        for name, domains in (
            ("includeDomains", include_domains),
            ("excludeDomains", exclude_domains),
        ):
            if domains:
                body[name] = public_domains(domains)
        body["contents"] = self._contents(content, query, fresh, links=0)
        maximum = exa_bound(depth, num_results, fresh, len(additional_queries))
        return self._request("/search", body, material_gap, maximum)

    def contents(self, source_id: str, material_gap: str) -> dict:
        """Full text for a saved public result, fetched live."""
        source = self.evidence.source(self.context.owner, self.context.assessment_id, source_id)
        if source.kind != "web":
            raise ValueError("Contents retrieval is limited to saved public web sources.")
        return self.fetch(source.origin, material_gap)

    def fetch(self, url: str, material_gap: str, *, links: int = EXA_LINKS) -> dict:
        """Fetch one admitted public URL live, saving its text and outgoing links."""
        if type(links) is not int or not 0 <= links <= EXA_LINKS:
            raise ValueError(f"Request between 0 and {EXA_LINKS} outgoing links.")
        body = {"urls": [public_url(url)]} | self._contents("text", None, True, links)
        return self._request("/contents", body, material_gap, exa_page_bound(fresh=True))

    @staticmethod
    def _contents(content: str, query: str | None, fresh: bool, links: int) -> dict:
        contents = (
            {"highlights": {"query": query, "maxCharacters": 4_000}}
            if content == "highlights"
            else {"text": {"maxCharacters": 12_000}}
        )
        if fresh:
            contents["maxAgeHours"] = 0
            contents["livecrawlTimeout"] = 15_000
        if links:
            contents["extras"] = {"links": links}
        return contents

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
                "pricing_source": "https://exa.ai/docs/reference/pricing",
            },
        )
        source_ids, found, rejected = [], [], []
        for index, item in enumerate(data["results"]):
            try:
                url = public_url(item["url"])
            except (ValueError, KeyError):
                rejected.append({"index": index, "reason": "Not an admissible public URL."})
                continue
            highlights = [part for part in item.get("highlights") or [] if isinstance(part, str)]
            text = (
                item.get("text")
                or "\n\n".join(highlights)
                or "Title: " + str(item.get("title", "Unavailable")) + "\nURL: " + url
            )
            limitations = ["Original bytes are the Exa response, not a publisher HTML archive."]
            if not item.get("text") and highlights:
                limitations.append(
                    "Query-focused highlights only; fetch the page for its full text."
                )
            elif not item.get("text"):
                limitations.append("No page text was returned; only source metadata is available.")
            links = [
                link
                for link in (item.get("extras") or {}).get("links") or []
                if isinstance(link, str)
            ][:EXA_LINKS]
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
                    "title": item.get("title"),
                    "live_fetch": path == "/contents" or "maxAgeHours" in body.get("contents", {}),
                    "links": links,
                    "public": True,
                },
                limitations=limitations,
                retrieved_at=datetime.fromisoformat(saved["retrieved_at"]),
            )
            source_ids.append(source.id)
            found.append(
                {
                    "source_id": source.id,
                    "url": url,
                    "title": item.get("title"),
                    "published_at": item.get("publishedDate"),
                    "outgoing_links": len(links),
                }
            )
        return {
            "operation_id": operation["id"],
            "source_ids": source_ids,
            "results": found,
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


@dataclass(frozen=True)
class ApifyActor:
    name: str
    start_usd: Decimal
    item_usd: Decimal


# Pay-per-event prices on the FREE tier, checked 2026-10-30. Each run passes the bound
# as Apify's maxTotalChargeUsd, so the provider itself refuses to bill above it.
APIFY_ACTORS = {
    "x": ApifyActor("xquik~x-tweet-scraper", Decimal("0"), Decimal("0.00015")),
    # $0.02 actor start per GB at 1 GB, $0.002 per saved post or comment.
    "reddit": ApifyActor("harshmaur~reddit-scraper", Decimal("0.02"), Decimal("0.002")),
    # $0.00275 per transcribed video plus $0.00001 per stored row and per start.
    "youtube": ApifyActor("johnvc~YoutubeTranscripts", Decimal("0.00001"), Decimal("0.00276")),
}
APIFY_MARGIN = Decimal("1.25")
# A very small maxTotalChargeUsd makes Actors return nothing: a $0.0002 cap on a one-tweet
# lookup produced an empty run, while $0.01 succeeded (2026-10-30). Never cap below this.
APIFY_MIN_CHARGE_USD = Decimal("0.01")
APIFY_HOSTS = {
    "x": {"x.com", "twitter.com", "mobile.twitter.com"},
    "reddit": {"reddit.com", "old.reddit.com", "np.reddit.com"},
    "youtube": {"youtube.com", "m.youtube.com", "youtu.be"},
}
X_FIELDS = [
    "id",
    "text",
    "url",
    "createdAt",
    "authorUsername",
    "likeCount",
    "replyCount",
    "retweetCount",
    "viewCount",
    "conversationId",
    "isReply",
    "inReplyToId",
    "quotedTweetId",
    "entities",
]
_TWEET_ID = re.compile(
    r"^(?:https://(?:www\.)?(?:x|twitter)\.com/\w+/status/)?(\d{5,25})(?:[/?].*)?$"
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")


def apify_bound(actor: ApifyActor, items: int) -> Decimal:
    bound = ((actor.start_usd + items * actor.item_usd) * APIFY_MARGIN).quantize(Decimal("0.0001"))
    return max(bound, APIFY_MIN_CHARGE_USD)


def platform_url(value: str, platform: str) -> str:
    url = public_url(value)
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    if host not in APIFY_HOSTS[platform]:
        raise ValueError(f"Expected a public {platform} URL.")
    return url


def tweet_id(value: str) -> str:
    match = _TWEET_ID.match(value.strip())
    if not match:
        raise ValueError("Use a numeric tweet ID or an x.com/twitter.com status URL.")
    return match.group(1)


def _public_links(values) -> list[str]:
    links = []
    for value in values:
        try:
            links.append(public_url(value))
        except (ValueError, TypeError):
            continue
    return list(dict.fromkeys(links))[:EXA_LINKS]


def _x_source(item: dict) -> dict:
    created = item.get("createdAt")
    try:
        published = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except (TypeError, ValueError):
        published = None
    metrics = ", ".join(
        f"{name} {item[key]}"
        for name, key in (
            ("likes", "likeCount"),
            ("replies", "replyCount"),
            ("reposts", "retweetCount"),
            ("views", "viewCount"),
        )
        if item.get(key) is not None
    )
    urls = (item.get("entities") or {}).get("urls") or []
    return {
        "origin": item.get("url"),
        "text": f"@{item.get('authorUsername')} on X, {created}:\n{item.get('text', '')}"
        + (f"\n\n({metrics})" if metrics else ""),
        "published_at": published,
        "title": f"@{item.get('authorUsername')} on X",
        "origin_group": f"x:conversation:{item.get('conversationId') or item.get('id')}",
        "links": _public_links(
            entry.get("expandedUrl") for entry in urls if isinstance(entry, dict)
        ),
    }


def _reddit_source(item: dict) -> dict:
    post = item.get("dataType", "post") == "post"
    origin = item.get("postUrl") if post else item.get("url") or item.get("postUrl")
    thread = item.get("postId") or item.get("parsedPostId") or item.get("id")
    heading = (
        f"{item.get('communityName')} post: {item.get('title', '')}"
        if post
        else f"{item.get('communityName')} comment by {item.get('authorName')}"
    )
    counts = (
        f"(upvotes {item.get('upVotes')}, comments {item.get('commentsCount')})" if post else ""
    )
    return {
        "origin": origin,
        "text": "\n\n".join(part for part in (heading, item.get("body") or "", counts) if part),
        "published_at": item.get("createdAt"),
        "title": item.get("title") or heading,
        "origin_group": f"reddit:{thread if post else item.get('postId') or thread}",
        "links": _public_links(
            [item.get("contentUrl")] if item.get("outboundUrlHost") != "reddit.com" else []
        ),
    }


def _youtube_source(item: dict) -> dict:
    transcript = item.get("text") or item.get("non_timestamped") or ""
    return {
        "origin": item.get("url"),
        "text": f"{item.get('title')} — {item.get('channel_name')} (YouTube)\n\n{transcript}",
        "published_at": None,
        "title": item.get("title"),
        "origin_group": f"youtube:{item.get('video_id')}",
        "links": _public_links(_URL_IN_TEXT.findall(item.get("description") or "")),
    }


PARSERS = {"x": _x_source, "reddit": _reddit_source, "youtube": _youtube_source}


class ApifyResearch:
    """X, Reddit and YouTube retrieval through selected Apify Actors and the ledger."""

    def __init__(
        self,
        context: ProviderContext,
        evidence: EvidenceStore,
        api_token: str,
        *,
        private_terms: tuple[str, ...] = (),
        transport=None,
    ):
        if not api_token:
            raise ValueError("An Apify token is required.")
        self.context, self.evidence = context, evidence
        self.private_terms = (api_token, context.owner, context.assessment_id, *private_terms)
        self.client = httpx.Client(
            base_url="https://api.apify.com/v2",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=180,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def x_posts(self, posts: tuple[str, ...], material_gap: str) -> dict:
        """Look up specific tweets by ID or status URL."""
        ids = list(dict.fromkeys(tweet_id(value) for value in posts))
        if not 1 <= len(ids) <= 20:
            raise ValueError("Look up 1-20 tweets at a time.")
        body = {"tweetIds": ids, "maxItems": len(ids)}
        return self._run("x", body | self._x_output(), len(ids), material_gap)

    def x_conversation(
        self, conversation_id: str, material_gap: str, *, max_items: int = 30
    ) -> dict:
        """Collect a conversation thread by its first tweet's ID."""
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise ValueError("Collect 1-100 tweets from a conversation.")
        body = {"conversationIds": [tweet_id(conversation_id)], "maxItems": max_items}
        return self._run("x", body | self._x_output(), max_items, material_gap)

    def x_search(
        self,
        query: str,
        material_gap: str,
        *,
        since: date | None = None,
        max_items: int = 30,
        sort: str = "Top",
    ) -> dict:
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise ValueError("Search 1-100 tweets at a time.")
        if sort not in {"Top", "Latest"}:
            raise ValueError("Sort must be Top or Latest.")
        body = {
            "searchTerms": [public_query(query, self.private_terms)],
            "maxItems": max_items,
            "queryType": sort,
        }
        if since:
            body["time"] = {"since": since.isoformat()}
        return self._run("x", body | self._x_output(), max_items, material_gap)

    def reddit(
        self,
        material_gap: str,
        *,
        thread_urls: tuple[str, ...] = (),
        query: str | None = None,
        community: str | None = None,
        posted_after: date | None = None,
        max_posts: int = 10,
        comments_per_post: int = 10,
    ) -> dict:
        """Read specific threads or search posts, with their top comments."""
        if not thread_urls and not query:
            raise ValueError("Supply thread URLs or a search query.")
        if type(max_posts) is not int or not 1 <= max_posts <= 25:
            raise ValueError("Read 1-25 posts per search or URL.")
        if type(comments_per_post) is not int or not 0 <= comments_per_post <= 30:
            raise ValueError("Read 0-30 comments per post.")
        urls = [platform_url(url, "reddit") for url in thread_urls][:5]
        body = {
            "startUrls": [{"url": url} for url in urls],
            "searchTerms": [public_query(query, self.private_terms)] if query else [],
            "searchSort": "relevance",
            "maxPostsCount": max_posts,
            "crawlCommentsPerPost": comments_per_post > 0,
            "maxCommentsPerPost": comments_per_post,
            "searchComments": False,
        }
        if community:
            if not re.fullmatch(r"(?:r/)?[A-Za-z0-9_]{2,21}", community):
                raise ValueError("Name one subreddit, such as r/selfhosted.")
            body["withinCommunity"] = community
        if posted_after:
            body["postedAfter"] = posted_after.isoformat()
        targets = len(urls) + (1 if query else 0)
        items = targets * max_posts * (1 + comments_per_post)
        return self._run("reddit", body, items, material_gap)

    def youtube_transcripts(self, videos: tuple[str, ...], material_gap: str) -> dict:
        urls = list(dict.fromkeys(platform_url(url, "youtube") for url in videos))
        if not 1 <= len(urls) <= 5:
            raise ValueError("Transcribe 1-5 videos at a time.")
        body = {
            "youtube_url": urls,
            "languages": ["en"],
            "output_formats": ["text"],
            "include_metadata": True,
        }
        return self._run("youtube", body, len(urls), material_gap)

    @staticmethod
    def _x_output() -> dict:
        return {"outputVariant": "rich", "fieldStyle": "camelCase", "fields": X_FIELDS}

    def _run(self, platform: str, body: dict, items: int, material_gap: str) -> dict:
        if not material_gap.strip():
            raise ValueError("Explain the material missing fact before purchasing new research.")
        actor = APIFY_ACTORS[platform]
        bound = apify_bound(actor, items)
        context = self.context
        key = "apify:" + content_hash([actor.name, body])
        params = {"timeout": 150, "memory": 1024, "maxTotalChargeUsd": str(bound)}

        def invoke():
            with self.client.stream(
                "POST", f"/acts/{actor.name}/run-sync-get-dataset-items", params=params, json=body
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("The provider response exceeded its admitted size.")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("The Actor did not return a dataset item list.")
            return ProviderResult({"items": data, "retrieved_at": datetime.now(UTC).isoformat()})

        operation = execute_once(
            context.state,
            owner=context.owner,
            assessment_id=context.assessment_id,
            generation=context.generation,
            key=key,
            kind="apify",
            request={"actor": actor.name, "body": body, "params": params},
            max_microusd=microusd(bound),
            dispatch_identity={
                "provider": "apify",
                "actor": actor.name,
                "local_request_key": key,
                "material_gap": material_gap,
            },
            invoke=invoke,
        )
        if operation["state"] != "succeeded":
            raise Conflict("The saved research operation failed; it will not be purchased again.")
        saved = operation["result"]
        context.state.record_pending_cost(
            context.owner,
            context.assessment_id,
            operation["id"],
            microusd(bound),
            {
                "basis": "Apify pay-per-event bound, enforced upstream by maxTotalChargeUsd; "
                "the run's actual charge is reconciled from Apify usage",
                "actor": actor.name,
                "pricing_source": f"https://apify.com/{actor.name.replace('~', '/')}/pricing",
            },
        )
        results, rejected = [], []
        for index, item in enumerate(saved["items"]):
            parsed = PARSERS[platform](item) if isinstance(item, dict) else {}
            try:
                origin = public_url(parsed.get("origin") or "")
            except ValueError:
                rejected.append({"index": index, "reason": "No admissible public URL."})
                continue
            limitations = [
                "Original bytes are the Actor's dataset item, not a platform archive.",
                "Platform posts are first-person accounts; verify claims before relying on them.",
            ]
            if platform == "youtube":
                limitations.append("Upload date was not retrieved; transcripts may be generated.")
            source = self.evidence.save_source(
                context.owner,
                context.assessment_id,
                kind="web",
                origin=origin,
                original=json.dumps(item, sort_keys=True).encode(),
                text=parsed["text"],
                media_type="application/json",
                provenance={
                    "provider": "apify",
                    "actor": actor.name,
                    "operation_id": operation["id"],
                    "result_index": index,
                    "published_at": parsed["published_at"],
                    "title": parsed["title"],
                    "origin_group": parsed["origin_group"],
                    "links": parsed["links"],
                    "public": True,
                },
                limitations=limitations,
                retrieved_at=datetime.fromisoformat(saved["retrieved_at"]),
            )
            results.append(
                {
                    "source_id": source.id,
                    "url": origin,
                    "title": parsed["title"],
                    "published_at": parsed["published_at"],
                    "origin_group": parsed["origin_group"],
                    "outgoing_links": len(parsed["links"]),
                }
            )
        notices = (
            []
            if saved["items"]
            else [
                "The Actor returned no items. This is not evidence of absence: the post may be "
                "deleted or private, or the query too narrow. Try another route or query."
            ]
        )
        return {
            "operation_id": operation["id"],
            "source_ids": [item["source_id"] for item in results],
            "results": results,
            "rejected_results": rejected,
            "provider_notices": notices,
            "cost_status": "pending",
            "reserved_microusd": microusd(bound),
        }
