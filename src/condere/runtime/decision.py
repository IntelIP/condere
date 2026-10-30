"""The decision step: a compact brief, typed Jev questions, then code-enforced rules.

Jev reads exactly the brief written from the lead's analysis and answers with
probabilities. Code maps those answers into `Decision` and applies the gates and
verdict rules. Thresholds below are calibration choices for Jev 1.14 and must be
re-checked when the decision model changes.
"""

import hashlib
import time

from condere.runtime.operations import ProviderRefused
from condere.runtime.spinoff import (
    Analysis,
    ArtifactLink,
    Decision,
    Route,
    Verdict,
    apply_decision_rules,
)

BRIEF_REF = "artifacts/decision-brief.md"
BRIEF_LIMIT = 30_000  # Under Jev's ~32,000-byte long-context threshold.
GATE_PASS, GATE_FAIL = 0.7, 0.3
RATINGS = ("strong", "moderate", "weak", "unknown")
CRITERIA = {
    "buyer_pain": "How strong is the evidence that the buyer has a painful, recurring problem "
    "this option solves?",
    "willingness_to_pay": "How strong is the evidence that buyers would pay for the paid part?",
    "differentiation": "How strong is the case that buyers would choose this over self-hosting "
    "the open part or an existing alternative?",
    "build_gap": "How favourable is the gap between the current code and a sellable product "
    "(strong means small and well understood)?",
    "economics": "How strong is the evidence that this option could be profitable?",
    "founder_fit": "How well could the founder build and operate this with known resources?",
    "reversibility": "How cheaply could the founder test this and back out if it fails?",
}
RATING_CRITERIA = {
    "strong": "Supported by observed or reported evidence in the brief.",
    "moderate": "Partly supported; important parts are modelled or assumed.",
    "weak": "The brief's evidence points against it.",
    "unknown": "The brief has no evidence either way.",
}
VERDICTS = {
    "pursue": "Evidence supports pursuing the best option with its next test.",
    "reshape": "There is something here, but the option needs a different buyer, boundary "
    "or route first.",
    "stop": "Rights block every option, or buyers show no pain worth paying to remove.",
    "insufficient_evidence": "The brief lacks the evidence needed to judge; more research is "
    "needed first.",
}


STATEMENT_LIMIT = 240


def _ids(ids) -> str:
    return ", ".join(ids) if ids else "none"


def render_brief(analysis: Analysis, prompt: str) -> str:
    """The exact text Jev reads; it is saved and linked so anyone can audit or replay it.

    Each claim appears once in the evidence section; sections cite claim IDs. Listing
    full claims under every option made a 39-claim analysis 43.5 KB (run 4, 2026-10-30).
    """
    asset, rights, buyer = analysis.asset, analysis.rights, analysis.buyer
    sections = [
        "# Decision brief",
        f"Original request: {prompt.strip()[:2_000]}",
        f"Thesis: {analysis.thesis}",
        f"Summary: {analysis.summary}",
        "## Asset",
        f"{asset.summary}\nOwnership: {asset.ownership}. Maturity: {asset.maturity}.\n"
        f"Operating burden evidence: {_ids(asset.operating_burden)}",
        "## Rights",
        f"{rights.summary}\nCan open-source: {rights.can_open_source}. "
        f"Can sell hosted: {rights.can_sell_hosted}.",
        *[
            f"Blocker: {blocker.issue} (resolve with {blocker.resolve_with})"
            for blocker in rights.blockers
        ],
        "## Buyer",
        f"{buyer.summary}\nUser: {buyer.user}. Buyer: {buyer.buyer}. Trigger: {buyer.trigger}. "
        f"Today: {buyer.current_solution}.\nEvidence: {_ids(buyer.claims)}",
        "## Options",
    ]
    for option in analysis.options:
        sections += [
            f"### {option.id}: {option.route}",
            f"Open: {option.open_part}\nPaid: {option.paid_part}\nBuyer: {option.buyer}\n"
            f"Why pay: {option.why_pay}\nMain risk: {option.main_risk}",
            f"Case for: {option.case_for}\nCase against: {option.case_against}",
            f"Evidence: {_ids(option.claims)}",
        ]
    sections.append("## Evidence")
    sections.append(
        "\n".join(
            f"- {claim.id} [{claim.status}] {claim.statement[:STATEMENT_LIMIT]}"
            for claim in analysis.claims
        )
    )
    if analysis.open_questions:
        sections.append("## Open questions")
        sections += [
            f"- {item.question} (blocks {item.blocks}; answerable by {item.who_can_answer})"
            for item in analysis.open_questions
        ]
    text = "\n\n".join(section for section in sections if section)
    if len(text.encode()) > BRIEF_LIMIT:
        raise ValueError(
            f"The decision brief would be {len(text.encode())} bytes, over {BRIEF_LIMIT}; "
            "shorten the summaries, option cases and claim statements."
        )
    return text


def brief_link(assessment_id: str, text: str) -> ArtifactLink:
    data = text.encode()
    return ArtifactLink(
        ref=BRIEF_REF,
        uri=f"condere://{assessment_id}/{BRIEF_REF}",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        media_type="text/markdown",
        summary="The exact brief the decision model read, written from the lead's analysis.",
    )


def option_questions(option_id: str) -> dict:
    questions = {
        "rights_clear": {
            "type": "noul",
            "instructions": f"For option {option_id}: do the brief's rights and license facts "
            "permit this option (open-sourcing, hosting or selling as described)? Answer no if "
            "a stated blocker prevents it.",
        },
        "asset_works": {
            "type": "noul",
            "instructions": f"For option {option_id}: does the brief show the software exists "
            "and works well enough to build this option on?",
        },
    }
    for criterion, question in CRITERIA.items():
        questions[criterion] = {
            "type": "choice",
            "instructions": f"For option {option_id}: {question}",
            "criteria": RATING_CRITERIA,
        }
    return questions


def overall_questions(analysis: Analysis) -> dict:
    options = {option.id: f"{option.route}: {option.paid_part}" for option in analysis.options}
    return {
        "verdict": {
            "type": "choice",
            "instructions": "Given the whole brief, what should the founder do next?",
            "criteria": VERDICTS,
        },
        "option": {
            "type": "choice",
            "instructions": "Which option has the strongest evidence-backed case?",
            "criteria": options | {"none": "No option is supported by the brief."},
        },
        "route": {
            "type": "choice",
            "instructions": "Which spin-off route best fits the brief's evidence?",
            "criteria": {route: route.replace("_", " ") for route in Route.__args__},
        },
    }


def _distribution(answer: dict, keys) -> dict:
    raw = {key: float((answer.get("probabilities") or {}).get(key, 0.0)) for key in keys}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("The decision model returned no probabilities.")
    return {key: value / total for key, value in raw.items()}


def _choice(answer: dict, keys) -> dict:
    distribution = _distribution(answer, keys)
    value = (
        answer.get("choice")
        if answer.get("choice") in keys
        else max(distribution, key=distribution.get)
    )
    return {"value": value, "distribution": distribution, "confidence": answer.get("confidence")}


REFUSAL_BACKOFF_SECONDS = (5, 20)


def _ask(jev, state: list[str], questions: dict, purpose: str, sleep=time.sleep) -> dict:
    """Ask again after a refusal, which bought nothing; an uncertain call is never repeated."""
    for attempt, pause in enumerate((*REFUSAL_BACKOFF_SECONDS, None)):
        try:
            return jev.ask(state, questions, purpose, attempt=attempt)
        except ProviderRefused:
            if pause is None:
                raise
            sleep(pause)
    raise AssertionError("unreachable")


def decide(jev, analysis: Analysis, prompt: str, assessment_id: str) -> tuple[Decision, str]:
    """Ask Jev, map its answers, and apply the code rules. Returns the decision and brief."""
    text = render_brief(analysis, prompt)
    gates, ratings, model = [], [], None
    for option in analysis.options:
        answered = _ask(jev, [text], option_questions(option.id), f"assess option {option.id}")
        model = answered["model"]
        answers = answered["answers"]
        for gate in ("rights_clear", "asset_works"):
            probability = float(answers[gate]["noul"])
            result = (
                "pass"
                if probability >= GATE_PASS
                else "fail"
                if probability <= GATE_FAIL
                else "unknown"
            )
            gates.append(
                {"option": option.id, "gate": gate, "p_yes": probability, "result": result}
            )
        for criterion in CRITERIA:
            choice = _choice(answers[criterion], RATINGS)
            ratings.append({"option": option.id, "criterion": criterion} | choice)
    answered = _ask(jev, [text], overall_questions(analysis), "choose verdict, option and route")
    answers = answered["answers"]
    raw = Decision.model_validate(
        {
            "verdict": _choice(answers["verdict"], Verdict.__args__),
            "route": _choice(answers["route"], Route.__args__),
            "option": _choice(
                answers["option"], [option.id for option in analysis.options] + ["none"]
            ),
            "gates": gates,
            "ratings": ratings,
            "model": answered["model"] or model,
            "brief": brief_link(assessment_id, text),
        }
    )
    return apply_decision_rules(analysis, raw), text
