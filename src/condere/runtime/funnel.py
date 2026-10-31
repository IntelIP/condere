"""The source funnel: how much retrieved material survives to the claims a decision rests on.

Stages, per research route: retrieval calls, sources saved, sources a saved candidate claim
cites, sources the analysis cites, and sources behind the claims the options rely on (what
the decision model weighs). A route that saves much and reaches the options rarely is where
retrieval can be narrowed; one that reaches them often is where it can widen.
"""

from collections import defaultdict

from condere.runtime.spinoff import Analysis

STAGES = ("saved", "claimed", "cited", "decisive")
APIFY_ROUTES = {"xquik": "x", "harshmaur": "reddit", "johnvc": "youtube"}
RESEARCH_KINDS = {"exa", "apify", "hackernews", "github"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be"}


def operation_route(operation: dict) -> str:
    request = operation.get("request") or {}
    if operation["kind"] == "apify":
        return APIFY_ROUTES.get(str(request.get("actor", "")).split("~")[0], "apify")
    if operation["kind"] != "exa":
        return operation["kind"]
    if request.get("path") == "/contents":
        return "exa:fetch"
    body = request.get("body") or {}
    if set(body.get("includeDomains") or ()) & YOUTUBE_HOSTS:
        return "exa:youtube"
    return "exa:" + str(body.get("type") or "auto")


def route_of(provenance: dict, operations: dict[str, dict]) -> str:
    operation = operations.get(str(provenance.get("operation_id")))
    if provenance.get("provider") in {"exa", "apify"} and operation is not None:
        return operation_route(operation)
    return provenance.get("provider") or "other"


def _cited(claims) -> set[str]:
    return {citation["source_id"] for claim in claims for citation in claim.get("citations", [])}


def source_funnel(
    sources: list[dict], operations: list[dict], candidates: list[dict], analysis: Analysis
) -> dict:
    """`sources` are saved source payloads (id, provenance); `candidates` candidate claims."""
    by_operation = {str(item["id"]): item for item in operations}
    routes = {
        source["id"]: route_of(source.get("provenance") or {}, by_operation) for source in sources
    }
    groups = {
        source["id"]: (source.get("provenance") or {}).get("origin_group") or source["id"]
        for source in sources
    }
    claims = [claim.model_dump(mode="json") for claim in analysis.claims]
    relied = {claim_id for option in analysis.options for claim_id in option.claims}
    stage_sources = {
        "saved": set(routes),
        "claimed": _cited(candidates),
        "cited": _cited(claims),
        "decisive": _cited(claim for claim in claims if claim["id"] in relied),
    }
    calls = defaultdict(int)
    for operation in operations:
        if operation["kind"] in RESEARCH_KINDS and operation["state"] == "succeeded":
            calls[operation_route(operation)] += 1
    by_route = defaultdict(lambda: {"calls": 0} | {stage: 0 for stage in STAGES})
    for route, count in calls.items():
        by_route[route]["calls"] = count
    for stage, ids in stage_sources.items():
        for source_id in ids:
            by_route[routes.get(source_id, "unknown")][stage] += 1
    for row in by_route.values():
        row["decisive_per_saved"] = (
            round(row["decisive"] / row["saved"], 3) if row["saved"] else None
        )
    used = {claim.get("candidate") for claim in claims if claim.get("candidate")}
    return {
        "stages": {
            stage: {
                "sources": len(ids),
                "origin_groups": len({groups.get(item, item) for item in ids}),
            }
            for stage, ids in stage_sources.items()
        },
        "claims": {
            "candidates": len(candidates),
            "candidates_used": len(used),
            # Code claims copied from an earlier assessment of the same commit, not re-derived.
            "candidates_reused": sum(1 for claim in candidates if claim.get("reused_from")),
            "analysis": len(claims),
            "relied_on_by_options": len(relied),
        },
        "routes": dict(sorted(by_route.items())),
    }
