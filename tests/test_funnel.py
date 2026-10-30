"""The source funnel counts each route's sources from retrieval to the options' claims."""

from test_spinoff import SOURCE, analysis

from condere.runtime.evaluation import funnel_scores
from condere.runtime.funnel import source_funnel
from condere.runtime.spinoff import Analysis

UNUSED = "c" * 64
FETCHED = "d" * 64


def test_funnel_follows_sources_by_route_to_the_options():
    operations = [
        {"id": 1, "kind": "apify", "state": "succeeded", "request": {"actor": "harshmaur~reddit"}},
        {"id": 2, "kind": "exa", "state": "succeeded", "request": {"body": {"type": "deep-lite"}}},
        {"id": 3, "kind": "exa", "state": "succeeded", "request": {"path": "/contents"}},
        {"id": 4, "kind": "exa", "state": "failed", "request": {"body": {"type": "auto"}}},
        {"id": 5, "kind": "openrouter", "state": "succeeded", "request": {}},
    ]
    sources = [
        {
            "id": SOURCE,
            "provenance": {"provider": "apify", "operation_id": 1, "origin_group": "r:1"},
        },
        {"id": UNUSED, "provenance": {"provider": "exa", "operation_id": 2}},
        {"id": FETCHED, "provenance": {"provider": "exa", "operation_id": 3}},
    ]
    candidates = [
        {"citations": [{"source_id": SOURCE}]},
        {"citations": [{"source_id": FETCHED}]},
    ]
    payload = analysis()
    payload["claims"][0]["candidate"] = "X0123456789"
    funnel = source_funnel(sources, operations, candidates, Analysis.model_validate(payload))

    assert funnel["stages"]["saved"] == {"sources": 3, "origin_groups": 3}
    assert funnel["stages"]["claimed"]["sources"] == 2
    assert funnel["stages"]["decisive"] == {"sources": 1, "origin_groups": 1}
    assert funnel["claims"] == {
        "candidates": 2,
        "candidates_used": 1,
        "candidates_reused": 0,
        "analysis": 3,
        "relied_on_by_options": 2,
    }
    routes = funnel["routes"]
    assert routes["reddit"] == {
        "calls": 1,
        "saved": 1,
        "claimed": 1,
        "cited": 1,
        "decisive": 1,
        "decisive_per_saved": 1.0,
    }
    assert routes["exa:deep-lite"]["saved"] == 1 and routes["exa:deep-lite"]["claimed"] == 0
    assert routes["exa:fetch"]["claimed"] == 1 and routes["exa:fetch"]["cited"] == 0
    assert "exa:auto" not in routes  # A failed call retrieved nothing.

    scores = {item["key"]: item["score"] for item in funnel_scores(funnel)}
    assert scores["citation_yield"] == round(1 / 3, 3)
    assert scores["candidate_use"] == 0.5
    assert scores["route_reddit_decisive"] == 1
