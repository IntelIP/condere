"""Code evaluators for a published spin-off result; they read saved records only.

Each evaluator returns a LangSmith-style `{"key", "score", "comment"}`. They check
structure, evidence discipline and freshness. They cannot judge whether the
analysis is right; that needs reviewed references or a model judge.
"""

import statistics
from datetime import UTC, datetime

from pydantic import ValidationError

from condere.runtime.decision import BRIEF_LIMIT
from condere.runtime.spinoff import EVIDENCED, SpinoffResult

RECENT_DAYS = 90


def _age_days(value, now: datetime) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds() / 86_400


def evaluate_result(
    payload: dict, claims: list[dict], sources: list[dict], verify, brief: str
) -> list[dict]:
    """Score one saved result. `verify(citation) -> bool` checks a quote against its source."""
    try:
        result = SpinoffResult.model_validate(payload)
    except ValidationError as error:
        return [{"key": "schema_valid", "score": 0, "comment": str(error)[:500]}]
    scores = [{"key": "schema_valid", "score": 1}]
    by_id = {claim["id"]: claim for claim in claims}
    evidenced = [claim for claim in claims if claim["status"] in EVIDENCED]
    scores.append(
        {
            "key": "evidenced_claim_share",
            "score": round(len(evidenced) / len(claims), 3) if claims else 0,
            "comment": f"{len(evidenced)} of {len(claims)} claims observed or reported",
        }
    )
    citations = [citation for claim in evidenced for citation in claim.get("citations", [])]
    verified = sum(1 for citation in citations if verify(citation))
    scores.append(
        {
            "key": "quotes_verified",
            "score": round(verified / len(citations), 3) if citations else 0,
            "comment": f"{verified} of {len(citations)} quotes match their saved sources",
        }
    )
    decision = result.decision
    chosen = next(
        (option for option in result.reasoning.options if option.id == decision.option.value),
        None,
    )
    supported = decision.verdict.value != "pursue" or (
        chosen is not None
        and any(by_id.get(claim, {}).get("status") in EVIDENCED for claim in chosen.claims)
    )
    scores.append(
        {
            "key": "verdict_supported",
            "score": int(supported),
            "comment": f"{decision.verdict.value} on {decision.option.value}",
        }
    )
    scores.append(
        {
            "key": "brief_within_limit",
            "score": int(len(brief.encode()) <= BRIEF_LIMIT),
            "comment": f"{len(brief.encode())} bytes",
        }
    )
    scores.append(
        {
            "key": "decision_adjusted",
            "score": len(decision.adjustments),
            "comment": "; ".join(item.reason for item in decision.adjustments) or "none",
        }
    )
    scores.append({"key": "contested", "score": len(decision.contested)})
    cited = {citation["source_id"] for claim in claims for citation in claim.get("citations", [])}
    index = {source["id"]: source for source in sources}
    now = result.created_at
    ages = [
        age
        for source_id in cited
        if (age := _age_days(index.get(source_id, {}).get("published_at"), now)) is not None
    ]
    scores += [
        {
            "key": "cited_dated_share",
            "score": round(len(ages) / len(cited), 3) if cited else 0,
            "comment": f"{len(ages)} of {len(cited)} cited sources carry a publication date",
        },
        {
            "key": "cited_recent_share",
            "score": round(sum(age <= RECENT_DAYS for age in ages) / len(ages), 3) if ages else 0,
            "comment": f"share of dated cited sources from the last {RECENT_DAYS} days",
        },
        {
            "key": "median_cited_age_days",
            "score": round(statistics.median(ages), 1) if ages else None,
        },
        {
            "key": "cited_provider_spread",
            "score": len({index.get(item, {}).get("provider") or "other" for item in cited}),
            "comment": ", ".join(
                sorted({index.get(item, {}).get("provider") or "other" for item in cited})
            ),
        },
        {
            "key": "independent_origins",
            "score": len(
                {
                    index.get(item, {}).get("origin_group") or index.get(item, {}).get("origin")
                    for item in cited
                }
            ),
            "comment": "cited sources after merging accounts of the same origin",
        },
    ]
    return scores
