"""Judges ask fixed rubric questions of a bounded case file and report probabilities."""

import json

import httpx
import pytest
from test_spinoff import result_payload

from condere.runtime.judges import (
    CASE_LIMIT,
    RUBRIC,
    JevJudge,
    agreement,
    case_file,
    citation_scores,
    jev_citations,
    jev_rubric,
)

CLAIMS = [
    {
        "id": "C1",
        "statement": "The project is MIT licensed and permits hosting.",
        "status": "observed",
        "citations": [{"quote": "Copyright (c) 2024 Alex Garcia"}],
    },
    {"id": "C2", "statement": "Buyers would pay.", "status": "modelled", "citations": []},
]


def jev(answer, asked: list) -> JevJudge:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "answers": {name: {"noul": answer(name)} for name in body["questions"]},
                "usage": {"cost": 0.0001},
            },
        )

    return JevJudge("key", transport=httpx.MockTransport(handler))


def test_case_file_adds_conclusions_and_shortens_evidence_to_fit():
    result = json.loads(json.dumps(result_payload(), default=str))
    case = case_file(result, "# Decision brief\n\n## Evidence\n\n- C1 [observed] short")
    assert "Verdict: pursue" in case and "## Next test" in case and "Build plan for O1" in case
    long = "## Evidence\n\n" + "\n".join(f"- C{i} [observed] " + "x" * 400 for i in range(70))
    trimmed = case_file(result, long)
    assert len(trimmed.encode()) <= CASE_LIMIT
    assert "- C69 [observed] " in trimmed
    with pytest.raises(ValueError):
        case_file(result, "y" * (CASE_LIMIT + 1))


def test_jev_answers_every_rubric_question_and_each_cited_quote():
    asked = []
    judge = jev(lambda name: 0.2 if name.startswith("C1") else 0.9, asked)
    scores = {item["key"]: item["score"] for item in jev_rubric(judge, "case")}
    assert set(scores) == {f"jev_{key}" for key in RUBRIC}
    supported = jev_citations(judge, CLAIMS)
    assert supported == {"C1_0": 0.2}  # Modelled claims cite nothing to check.
    assert asked[1]["state"][0].count("Quoted source text") == 1
    summary = citation_scores(supported)[0]
    assert summary["score"] == 0.2 and "C1_0" in summary["comment"]
    assert judge.cost_usd == pytest.approx(0.0002)


def test_agreement_reports_the_gap_per_question():
    result = agreement(
        [
            {"key": "jev_thesis_specific", "score": 0.3},
            {"key": "jev_options_distinct", "score": 0.8},
        ],
        [
            {"key": "llm_thesis_specific", "score": 0.5},
            {"key": "llm_options_distinct", "score": 0.8},
        ],
    )
    assert result == {"gaps": {"thesis_specific": 0.2, "options_distinct": 0.0}, "mean_gap": 0.1}
