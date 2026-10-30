"""Render the spin-off artifacts and the compact result from saved records; no model calls.

The compact JSON is what a coding agent reads first. Everything bulky (full
reasoning, section detail, the claims table, the source index, the decision
brief) is an artifact with a stable path, a hash and a one-line summary.
"""

import hashlib
import json

from condere.runtime.spinoff import (
    Analysis,
    ArtifactLink,
    BuildPlan,
    Claim,
    Decision,
)


def _claim_lines(analysis: Analysis, ids) -> list[str]:
    lines = []
    for claim_id in ids:
        claim = analysis.claim(claim_id)
        lines.append(f"- **{claim.id}** [{claim.status}] {claim.statement}")
        lines += [f"  - `{item.source_id[:12]}`: “{item.quote}”" for item in claim.citations]
    return lines


def _reasoning(analysis: Analysis, decision: Decision) -> str:
    lines = [
        "# Reasoning",
        "",
        f"**Thesis.** {analysis.thesis}",
        "",
        analysis.summary,
        "",
        f"**Decision.** {decision.verdict.value} — option {decision.option.value}, "
        f"route {decision.route.value}. The decision model read `decision-brief.md`.",
    ]
    if decision.adjustments:
        lines += ["", "Code adjustments to the decision model's answer:"]
        lines += [
            f"- {item.field}: {item.original} → {item.adjusted} ({item.reason})"
            for item in decision.adjustments
        ]
    if decision.contested:
        lines += ["", f"Contested answers: {', '.join(decision.contested)}."]
    for option in analysis.options:
        ratings = {
            item.criterion: item.value for item in decision.ratings if item.option == option.id
        }
        lines += [
            "",
            f"## {option.id}: {option.route}",
            f"- Open: {option.open_part}",
            f"- Paid: {option.paid_part}",
            f"- Buyer: {option.buyer}",
            f"- Why pay: {option.why_pay}",
            f"- Main risk: {option.main_risk}",
            f"- Ratings: {', '.join(f'{key} {value}' for key, value in ratings.items())}",
            "",
            f"**Case for.** {option.case_for}",
            "",
            f"**Case against.** {option.case_against}",
            "",
            "Evidence:",
            *_claim_lines(analysis, option.claims),
        ]
    if analysis.open_questions:
        lines += ["", "## Open questions"]
        lines += [
            f"- {item.question} (blocks {item.blocks}; {item.who_can_answer})"
            for item in analysis.open_questions
        ]
    return "\n".join(lines) + "\n"


def _asset(analysis: Analysis) -> str:
    asset = analysis.asset
    lines = [
        "# Asset",
        "",
        asset.summary,
        "",
        f"Ownership: {asset.ownership}. Maturity: {asset.maturity}.",
        "",
        "## Repositories",
        *[f"- {item.url} at `{item.commit}` ({item.license})" for item in asset.repositories],
        "",
        "## Components",
        *[
            f"- **{item.id}** `{item.path}` ({item.value}): {item.role}"
            for item in asset.components
        ],
        "",
        "## Hard to replicate",
        *_claim_lines(analysis, asset.hard_to_replicate),
        "",
        "## Operating burden",
        *_claim_lines(analysis, asset.operating_burden),
    ]
    return "\n".join(lines) + "\n"


def _rights(analysis: Analysis) -> str:
    rights = analysis.rights
    lines = [
        "# Rights",
        "",
        rights.summary,
        "",
        f"Can open-source: {rights.can_open_source}. Can sell hosted: {rights.can_sell_hosted}.",
        "",
        "## Blockers",
        *[
            f"- {item.issue}"
            + (f" ({item.component})" if item.component else "")
            + f" — resolve with {item.resolve_with}"
            for item in rights.blockers
        ],
    ]
    if rights.license_recommendation:
        recommendation = rights.license_recommendation
        lines += [
            "",
            "## License recommendation",
            f"- Open part: {recommendation.open}",
            f"- Paid part: {recommendation.paid}",
            *_claim_lines(analysis, recommendation.claims),
        ]
    return "\n".join(lines) + "\n"


def _buyer(analysis: Analysis) -> str:
    buyer = analysis.buyer
    lines = [
        "# Buyer",
        "",
        buyer.summary,
        "",
        f"- User: {buyer.user}",
        f"- Buyer: {buyer.buyer}",
        f"- Trigger: {buyer.trigger}",
        f"- Current solution: {buyer.current_solution}",
        "",
        "## Evidence",
        *_claim_lines(analysis, buyer.claims),
    ]
    return "\n".join(lines) + "\n"


def _json(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_artifacts(
    analysis: Analysis, decision: Decision, brief: str, sources: list[dict]
) -> dict[str, tuple[str, str, str]]:
    """Every artifact as ref -> (text, media type, one-line summary)."""
    claims: list[Claim] = analysis.claims
    observed = sum(1 for claim in claims if claim.status in {"observed", "reported"})
    return {
        "artifacts/reasoning.md": (
            _reasoning(analysis, decision),
            "text/markdown",
            f"Case for and against {len(analysis.options)} options, with claim evidence.",
        ),
        "artifacts/asset.md": (
            _asset(analysis),
            "text/markdown",
            f"What the IP is: {len(analysis.asset.components)} components and operating burden.",
        ),
        "artifacts/rights.md": (
            _rights(analysis),
            "text/markdown",
            f"Ownership, licenses and {len(analysis.rights.blockers)} blockers.",
        ),
        "artifacts/buyer.md": (
            _buyer(analysis),
            "text/markdown",
            "Who has the problem, who pays and what they use today.",
        ),
        "artifacts/claims.json": (
            _json([claim.model_dump(mode="json") for claim in claims]),
            "application/json",
            f"{len(claims)} claims; {observed} observed or reported with exact quotes.",
        ),
        "artifacts/sources/index.json": (
            _json(sources),
            "application/json",
            f"{len(sources)} saved sources with publication dates and origin groups.",
        ),
        "artifacts/decision-brief.md": (
            brief,
            "text/markdown",
            "The exact brief the decision model read, written from the analysis.",
        ),
    }


def artifact_link(assessment_id: str, ref: str, text: str, media_type: str, summary: str):
    data = text.encode()
    return ArtifactLink(
        ref=ref,
        uri=f"condere://{assessment_id}/{ref}",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        media_type=media_type,
        summary=summary,
    )


def build_result(
    *,
    assessment_id: str,
    created_at,
    brief: dict,
    repositories: list[str],
    analysis: Analysis,
    decision: Decision,
    build_plan: BuildPlan | None,
    links: dict[str, ArtifactLink],
    source_count: int,
) -> dict:
    """The compact `condere.spinoff.v1` payload; validated by the caller."""
    return {
        "assessment_id": assessment_id,
        "created_at": created_at,
        "input": {
            "prompt": brief["original_input"],
            "repositories": repositories,
            "urls": brief.get("public_references", []),
        },
        "decision": decision.model_dump(mode="json"),
        "reasoning": {
            "thesis": analysis.thesis,
            "summary": analysis.summary,
            "options": [
                {
                    "id": option.id,
                    "case_for": option.case_for,
                    "case_against": option.case_against,
                    "claims": option.claims,
                }
                for option in analysis.options
            ],
            "full": links["artifacts/reasoning.md"].model_dump(),
        },
        "analysis": {
            name: {
                "summary": getattr(analysis, name).summary,
                "full": links[f"artifacts/{name}.md"].model_dump(),
            }
            for name in ("asset", "rights", "buyer")
        },
        "next_test": analysis.next_test.model_dump(mode="json"),
        "build_plan": build_plan.model_dump(mode="json") if build_plan else None,
        "open_questions": [item.model_dump(mode="json") for item in analysis.open_questions],
        "claims": {
            "count": len(analysis.claims),
            "full": links["artifacts/claims.json"].model_dump(),
        },
        "sources": {
            "count": source_count,
            "full": links["artifacts/sources/index.json"].model_dump(),
        },
        "artifacts": [link.model_dump() for link in links.values()],
    }
