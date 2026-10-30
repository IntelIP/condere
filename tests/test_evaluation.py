"""Code evaluators score structure, evidence discipline and freshness of a saved result."""

from datetime import UTC, datetime, timedelta

from test_spinoff import SOURCE, analysis, result_payload

from condere.runtime.evaluation import evaluate_result


def scores(payload, claims, sources, verify=lambda citation: True, brief="brief"):
    return {
        item["key"]: item["score"]
        for item in evaluate_result(payload, claims, sources, verify, brief)
    }


def test_scores_evidence_freshness_and_spread_without_research():
    payload = result_payload()
    claims = analysis()["claims"]
    recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    sources = [{"id": SOURCE, "published_at": recent, "provider": "apify", "origin_group": "x:1"}]
    result = scores(payload, claims, sources)
    assert result["schema_valid"] == 1
    assert result["evidenced_claim_share"] == round(1 / 3, 3)
    assert result["quotes_verified"] == 1
    assert result["verdict_supported"] == 1
    assert result["cited_dated_share"] == 1 and result["cited_recent_share"] == 1
    assert result["cited_provider_spread"] == 1 and result["independent_origins"] == 1


def test_failed_quotes_unsupported_pursue_and_invalid_schema_are_visible():
    payload = result_payload()
    claims = analysis()["claims"]
    for claim in claims:
        if claim["id"] == "C1":
            claim["status"] = "modelled"
            claim["citations"] = []
    result = scores(payload, claims, [], verify=lambda citation: False)
    assert result["verdict_supported"] == 0  # pursue on O1 with no evidenced claim
    assert result["quotes_verified"] == 0
    broken = scores(payload | {"schema_version": "other"}, claims, [])
    assert broken == {"schema_valid": 0}
