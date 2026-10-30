"""Model judges for published spin-off results: Jev and GPT-6 Astra on every run, Opus to calibrate.

Code evaluators (evaluation.py) check structure and evidence discipline; they cannot tell
whether the analysis is useful. These judges answer the same rubric questions, from the same
case file, as probabilities in [0, 1]. Jev is cheap enough to judge every run. GPT-6 Astra
answers them on the owner's ChatGPT plan (owner approval 2026-10-30, after a TypeSafe
outage stopped Jev), so every run gets a second judge at no per-call charge and keeps a
judge when Jev is unavailable. The OpenEvals Opus judge remains an optional paid check.
None replaces the owner's review; all are operator evaluation, outside any assessment's
spending ledger.
"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

JUDGE_JEV_MODEL = "typesafe/jev-1.14"
JUDGE_LLM_MODEL = "anthropic/claude-opus-5.5"
JUDGE_PLAN_MODEL = "gpt-6-astra"
CASE_LIMIT = 31_000  # Jev's state stays under its ~32,000-byte long-context threshold.
QUOTE_LIMIT = 600

# What a useful spin-off answer does, as yes/no questions about the case file.
RUBRIC = {
    "thesis_specific": "Is the thesis specific: it names who pays, for what capability, and "
    "why they would pay now, rather than a generic statement that could fit any project?",
    "options_distinct": "Are the options genuinely different spin-off routes, buyers or "
    "open/paid boundaries, rather than variations of one idea?",
    "cases_grounded": "Does each option's case for and against rest on the evidence claims it "
    "lists, rather than on assertion or on claims that do not bear on it?",
    "buyer_evidence_firsthand": "Does the buyer evidence come from the people who would buy or "
    "use the product (practitioners, users, buyers, their spending), rather than from vendors, "
    "marketing or the analyst's own inference?",
    "decision_follows": "Do the verdict and chosen option follow from the evidence in the case, "
    "with real disagreements or weak spots acknowledged rather than hidden?",
    "gaps_acknowledged": "Are material unknowns stated as unknowns or open questions, rather "
    "than filled with assumptions presented as facts?",
    "next_test_actionable": "Is the next test cheap, specific and decisive: a named method, a "
    "clear success and failure signal, and a cost and time a founder could act on this week?",
    "handoff_actionable": "Could a coding agent act on the result (the next test, and the build "
    "plan if present) without redoing the research or guessing what to build?",
}


def case_file(result: dict, brief: str) -> str:
    """The judged text: the decision brief plus what the decision and plan concluded."""
    decision = result["decision"]
    lines = [
        brief,
        "## Decision (made by the decision model; judge it against the evidence above)",
        f"Verdict: {decision['verdict']['value']} "
        f"(confidence {decision['verdict'].get('confidence')}). "
        f"Option: {decision['option']['value']}. Route: {decision['route']['value']}.",
    ]
    if decision.get("contested"):
        lines.append("Contested: " + "; ".join(decision["contested"]))
    lines += [
        f"Code adjustment: {item.get('reason') or item}" for item in decision.get("adjustments", [])
    ]
    test = result["next_test"]
    lines += [
        "## Next test",
        f"Assumption: {test['assumption']}\nMethod: {test['method']}\n"
        f"Success: {test['success']}\nFailure: {test['failure']}\n"
        f"Cost and time: {test['cost_and_time']}",
    ]
    plan = result.get("build_plan")
    if plan:
        lines.append("## Build plan for " + str(plan.get("option")))
        lines.append("Keep open: " + ", ".join(plan.get("keep_open", [])))
        lines += [
            f"- {item.get('id')}: {item.get('goal')} (accept: {item.get('acceptance')})"
            for item in plan.get("work_packages", [])
        ]
    text = "\n\n".join(lines)
    # A brief near its own limit plus the conclusions can exceed Jev's; shorten evidence lines
    # (claim statements) before anything else, since claim IDs and statuses carry the structure.
    for width in (200, 160, 120, 90):
        if len(text.encode()) <= CASE_LIMIT:
            return text
        text = re.sub(
            r"^(- C\d+ \[\w+\] )(.*)$",
            lambda match, width=width: match.group(1) + match.group(2)[:width],
            text,
            flags=re.MULTILINE,
        )
    if len(text.encode()) > CASE_LIMIT:
        raise ValueError(f"The case file is {len(text.encode())} bytes, over {CASE_LIMIT}.")
    return text


class JevJudge:
    """Jev through OpenRouter's System One endpoint; answers are probabilities, never prose."""

    def __init__(self, api_key: str, *, transport=None):
        self.client = httpx.Client(
            base_url="https://openrouter.ai/api/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
            transport=transport,
        )
        self.cost_usd = 0.0

    def close(self) -> None:
        self.client.close()

    def ask(self, state: list[str], questions: dict) -> dict:
        body = {"model": JUDGE_JEV_MODEL, "state": state, "questions": questions}
        response = self.client.post("/systemone", json=body)
        response.raise_for_status()
        data = response.json()
        answers = data.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("Jev did not answer exactly the questions asked.")
        self.cost_usd += float((data.get("usage") or {}).get("cost") or 0)
        return answers


def jev_rubric(jev: JevJudge, case: str) -> list[dict]:
    answers = jev.ask(
        [case],
        {key: {"type": "noul", "instructions": question} for key, question in RUBRIC.items()},
    )
    return [{"key": f"jev_{key}", "score": round(float(answers[key]["noul"]), 3)} for key in RUBRIC]


def jev_citations(jev: JevJudge, claims: list[dict], batch: int = 30) -> dict:
    """Ask, for each cited quote, whether it supports its claim's statement.

    Exact quotes are already verified by code; this checks the step code cannot: that the
    quote says what the claim says it does.
    """
    pairs = citation_pairs(claims)
    supported = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start : start + batch]
        state = [
            "\n\n".join(
                f"[{name}]\nStatement: {statement}\nQuoted source text: {quote}"
                for name, statement, quote in chunk
            )
        ]
        answers = jev.ask(
            state,
            {
                name: {
                    "type": "noul",
                    "instructions": f"For item {name}: does the quoted source text, read on its "
                    "own, support the statement (not merely mention the same topic)?",
                }
                for name, _, _ in chunk
            },
        )
        supported |= {name: float(answers[name]["noul"]) for name, _, _ in chunk}
    return supported


def citation_pairs(claims: list[dict]) -> list[tuple[str, str, str]]:
    return [
        (f"{claim['id']}_{index}", claim["statement"], citation["quote"][:QUOTE_LIMIT])
        for claim in claims
        if claim.get("status") in {"observed", "reported"}
        for index, citation in enumerate(claim.get("citations", []))
    ]


def llm_citations(judge_model, claims: list[dict], batch: int = 30) -> dict:
    """The same quote-support question answered by the LLM judge, for calibrating Jev."""
    pairs = citation_pairs(claims)
    supported = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start : start + batch]
        schema = {
            "title": "quote_support",
            "description": "For each item, the probability that the quote supports the statement.",
            "type": "object",
            "properties": {name: {"type": "number"} for name, _, _ in chunk},
            "required": [name for name, _, _ in chunk],
        }
        answer = judge_model.with_structured_output(schema).invoke(
            "For each item, give the probability (0 to 1) that the quoted source text, read on "
            "its own, supports the whole statement, not merely the same topic or one part of "
            "it.\n\n"
            + "\n\n".join(
                f"[{name}]\nStatement: {statement}\nQuoted source text: {quote}"
                for name, statement, quote in chunk
            )
        )
        supported |= {name: float(answer[name]) for name, _, _ in chunk}
    return supported


def citation_scores(supported: dict, prefix: str = "jev") -> list[dict]:
    if not supported:
        return [{"key": f"{prefix}_quote_supports_claim", "score": None, "comment": "None."}]
    weak = sorted(name for name, p in supported.items() if p < 0.5)
    return [
        {
            "key": f"{prefix}_quote_supports_claim",
            "score": round(sum(supported.values()) / len(supported), 3),
            "comment": f"{len(supported)} quotes; below 0.5: {', '.join(weak) or 'none'}.",
        }
    ]


LLM_PROMPT = """You are judging an analysis of how to spin a business off a piece of software.
The case file below is everything the decision was made from, followed by the decision, the
next test and the build plan. Judge only what the case file shows.

<question>
{question}
</question>

<case_file>
{outputs}
</case_file>

Think it through, then give a score between 0 and 1: 1 means clearly yes, 0 means clearly no."""


def llm_rubric(judge_model, case: str, invoke: Callable | None = None) -> list[dict]:
    """The same rubric through an OpenEvals LLM-as-judge (continuous score with reasoning)."""
    from openevals.llm import create_llm_as_judge

    scores = []
    for key, question in RUBRIC.items():
        evaluator = invoke or create_llm_as_judge(
            prompt=LLM_PROMPT.replace("{question}", question),
            judge=judge_model,
            continuous=True,
            feedback_key=f"llm_{key}",
        )
        outcome = evaluator(outputs=case)
        scores.append(
            {
                "key": f"llm_{key}",
                "score": round(float(outcome["score"]), 3),
                "comment": str(outcome.get("comment") or "")[:1_000],
            }
        )
    return scores


def agreement(
    first: list[dict], second: list[dict], prefixes: tuple[str, str] = ("jev", "llm")
) -> dict:
    """Per-question gap between two judges; large gaps mark questions a judge cannot answer."""
    one = {item["key"].removeprefix(prefixes[0] + "_"): item["score"] for item in first}
    two = {item["key"].removeprefix(prefixes[1] + "_"): item["score"] for item in second}
    gaps = {key: round(abs(one[key] - two[key]), 3) for key in RUBRIC if key in one and key in two}
    return {"gaps": gaps, "mean_gap": round(sum(gaps.values()) / len(gaps), 3) if gaps else None}


# What low, middling and high answers look like for each rubric question. A single
# all-questions call without anchors rated hedged theses 0.87-0.93 where Jev gave 0.14-0.36
# and Opus 0.45-0.65 (2026-10-30), so Astra scores one question at a time against these.
ANCHORS = {
    "thesis_specific": "0.1: no buyer, or 'X could support a business' with demand left open. "
    "0.5: names a buyer or a paid capability but not both, or no reason to pay now. "
    "0.9: a specific buyer, the paid capability (not the open code) and the trigger to pay now.",
    "options_distinct": "0.1: the options differ only in wording or price. 0.5: two share a "
    "buyer and boundary. 0.9: each differs in route, buyer or open/paid boundary.",
    "cases_grounded": "0.1: cases assert without claim IDs or cite claims that do not bear on "
    "them. 0.5: some points rest on cited claims, others on assertion. 0.9: every point for and "
    "against rests on a listed claim that supports it.",
    "buyer_evidence_firsthand": "0.1: buyer evidence is vendor pages, marketing or the "
    "analyst's inference. 0.5: some practitioner accounts, mostly secondary. 0.9: buyers or "
    "users describe their own pain, workarounds or spending.",
    "decision_follows": "0.1: the verdict or option contradicts the evidence or ignores a "
    "failed gate. 0.5: it is plausible but weak spots are not acknowledged. 0.9: it follows "
    "from the evidence and names the disagreements that remain.",
    "gaps_acknowledged": "0.1: unknowns are filled with assumptions stated as facts. 0.5: some "
    "gaps are named, others hidden. 0.9: every material unknown is an unknown claim or open "
    "question.",
    "next_test_actionable": "0.1: vague ('validate demand'), no signal or cost. 0.5: a method "
    "but unclear success or failure, or costly. 0.9: a named, cheap method with clear success "
    "and failure signals a founder could run this week.",
    "handoff_actionable": "0.1: a coding agent would have to redo the research or guess what to "
    "build. 0.5: the next test is clear but the build plan is vague or missing. 0.9: the next "
    "test and the build plan's work packages could be started directly.",
}
PREAMBLE = (
    "You judge an analysis of how to spin a business off a piece of software. Judge only what "
    "the case file shows; do not reward length, polish or confident tone. Reason first, citing "
    "the parts of the case file that decide the question, then score 0 to 1 using the anchors."
)


class PlanJudge:
    """GPT-6 Astra on the owner's ChatGPT plan, answering with a strict JSON schema."""

    def __init__(self, credentials, *, model: str = JUDGE_PLAN_MODEL, client_factory=None):
        from openai import OpenAI

        from condere.runtime.chatgpt_plan import RESOURCE

        self.model = model
        self._client = client_factory or (
            lambda: OpenAI(
                api_key=credentials.access_token(),
                base_url=RESOURCE,
                max_retries=0,
                timeout=httpx.Timeout(300, read=120, connect=15),
            )
        )

    def answer(self, prompt: str, properties: dict) -> dict:
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        text = ""
        with self._client().responses.create(
            model=self.model,
            input=[{"role": "user", "content": prompt}],
            store=False,
            stream=True,
            reasoning={"effort": "medium"},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "judgement",
                    "schema": schema,
                    "strict": True,
                }
            },
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    text += event.delta
        return json.loads(text)

    def scores(self, prompt: str, names: list[str]) -> dict:
        answer = self.answer(prompt, {name: {"type": "number"} for name in names})
        return {name: _unit(answer[name]) for name in names}


def _unit(value) -> float:
    return min(1.0, max(0.0, float(value)))


def _parallel(calls: dict) -> dict:
    with ThreadPoolExecutor(max_workers=len(calls) or 1) as pool:
        futures = {key: pool.submit(call) for key, call in calls.items()}
        return {key: future.result() for key, future in futures.items()}


def plan_rubric(judge: PlanJudge, case: str) -> list[dict]:
    """One question per request, reasoning before an anchored score; requests run in parallel."""

    def ask(key):
        return lambda: judge.answer(
            f"{PREAMBLE}\n\nQuestion: {RUBRIC[key]}\nAnchors: {ANCHORS[key]}"
            f"\n\n<case_file>\n{case}\n</case_file>",
            {"reasoning": {"type": "string"}, "score": {"type": "number"}},
        )

    answers = _parallel({key: ask(key) for key in RUBRIC})
    return [
        {
            "key": f"astra_{key}",
            "score": round(_unit(answers[key]["score"]), 3),
            "comment": str(answers[key]["reasoning"])[:1_000],
        }
        for key in RUBRIC
    ]


def plan_comparison(judge: PlanJudge, case_a: str, case_b: str) -> dict:
    """For each question, the probability that A answers it better than B.

    Each question is asked with A first and again with B first; the two answers are averaged
    so the order of presentation cannot decide the comparison. Comparing two runs directly is
    less sensitive to a judge's overall leniency than absolute scores.
    """
    first, second = "first", "second"

    def ask(key, one, two):
        return lambda: judge.answer(
            f"{PREAMBLE}\n\nCompare two case files on this question: {RUBRIC[key]}\n"
            f"Anchors for a single file: {ANCHORS[key]}\n\nGive `prefer_first`, the "
            "probability that the first file answers the question better (0.5 when they are "
            f"equal).\n\n<{first}>\n{one}\n</{first}>\n\n<{second}>\n{two}\n</{second}>",
            {"reasoning": {"type": "string"}, "prefer_first": {"type": "number"}},
        )

    calls = {(key, "ab"): ask(key, case_a, case_b) for key in RUBRIC}
    calls |= {(key, "ba"): ask(key, case_b, case_a) for key in RUBRIC}
    answers = _parallel(calls)
    return {
        key: round(
            (
                _unit(answers[key, "ab"]["prefer_first"])
                + 1
                - _unit(answers[key, "ba"]["prefer_first"])
            )
            / 2,
            3,
        )
        for key in RUBRIC
    }


def plan_citations(judge: PlanJudge, claims: list[dict], batch: int = 30) -> dict:
    """The quote-support question for every cited quote, in batches."""
    pairs = citation_pairs(claims)
    supported = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start : start + batch]
        supported |= judge.scores(
            "For each item, give the probability (0 to 1) that the quoted source text, read on "
            "its own, supports the whole statement, not merely the same topic or one part of "
            "it. 0.1: the quote is on the topic but states none of the claim. 0.5: it states "
            "part of the claim. 0.9: it states the whole claim.\n\n"
            + "\n\n".join(
                f"[{name}]\nStatement: {statement}\nQuoted source text: {quote}"
                for name, statement, quote in chunk
            ),
            [name for name, _, _ in chunk],
        )
    return supported
