"""The decision step maps typed answers and applies code rules; Jev's judgement is not tested."""

import hashlib

import pytest
from test_spinoff import ASSESSMENT, analysis

from condere.runtime.decision import BRIEF_LIMIT, CRITERIA, _ask, decide, render_brief
from condere.runtime.operations import ProviderRefused, ProviderUncertain
from condere.runtime.spinoff import Analysis


class ScriptedJev:
    """Answers like Jev: probabilities only. Gate answers are set per option."""

    def __init__(self, gates: dict, verdict="pursue", option="O1"):
        self.gates, self.verdict, self.option, self.asked = gates, verdict, option, []

    def ask(self, state, questions, purpose, attempt=0):
        self.asked.append((state, questions, purpose))
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                option = purpose.rsplit(" ", 1)[-1]
                answers[name] = {"type": "noul", "noul": self.gates[option].get(name, 0.9)}
            else:
                keys = list(question["criteria"])
                chosen = {"verdict": self.verdict, "option": self.option}.get(name, keys[0])
                chosen = chosen if chosen in keys else keys[0]
                answers[name] = {
                    "type": "choice",
                    "choice": chosen,
                    "probabilities": {
                        key: 0.8 if key == chosen else 0.2 / (len(keys) - 1) for key in keys
                    },
                    "confidence": 0.7,
                }
        return {"model": "typesafe/jev-1.14-20261014", "answers": answers}


def test_decide_asks_per_option_then_overall_and_links_the_exact_brief():
    jev = ScriptedJev({"O1": {}, "O2": {}})
    decision, brief = decide(jev, Analysis.model_validate(analysis()), "Assess it.", ASSESSMENT)
    assert [purpose for _, _, purpose in jev.asked] == [
        "assess option O1",
        "assess option O2",
        "choose verdict, option and route",
    ]
    assert all(state == [brief] for state, _, _ in jev.asked)
    assert len(jev.asked[0][1]) == 2 + len(CRITERIA)
    assert decision.verdict.value == "pursue" and decision.option.value == "O1"
    assert decision.route.value == "managed_service"  # follows the option, not the raw answer
    assert decision.brief.sha256 == hashlib.sha256(brief.encode()).hexdigest()
    assert len(decision.ratings) == 2 * len(CRITERIA)
    assert "C1 [observed]" in brief and "Case against" in brief


def test_an_uncertain_gate_is_contested_not_a_third_verdict():
    jev = ScriptedJev({"O1": {"rights_clear": 0.5}, "O2": {}})
    decision, _ = decide(jev, Analysis.model_validate(analysis()), "Assess it.", ASSESSMENT)
    gate = next(
        item for item in decision.gates if item.option == "O1" and item.gate == "rights_clear"
    )
    assert gate.result == "unknown"
    assert decision.verdict.value in {"pursue", "stop"}
    assert any("unresolved hard gate" in item for item in decision.contested)


def test_failed_gate_moves_the_choice():
    jev = ScriptedJev({"O1": {"rights_clear": 0.1}, "O2": {}})
    decision, _ = decide(jev, Analysis.model_validate(analysis()), "Assess it.", ASSESSMENT)
    assert decision.option.value == "O2"


def test_oversized_brief_is_refused_before_any_request():
    long = analysis(summary="x" * 1_400)
    long["options"] = [
        option | {"case_for": "y" * 9_000, "case_against": "z" * 9_000}
        for option in long["options"]
    ]
    with pytest.raises(ValueError, match="over 30000"):
        render_brief(Analysis.model_validate(long), "Assess it.")
    assert BRIEF_LIMIT < 32_000


def test_a_refused_decision_call_is_asked_again_and_an_uncertain_one_is_not():
    class Flaky:
        def __init__(self, error):
            self.error, self.attempts = error, []

        def ask(self, state, questions, purpose, attempt=0):
            self.attempts.append(attempt)
            if attempt == 0:
                raise self.error
            return {"answers": {}, "model": "jev"}

    refused = Flaky(ProviderRefused("jev", 529, "overloaded"))
    assert _ask(refused, ["brief"], {}, "verdict", sleep=lambda _: None)["model"] == "jev"
    assert refused.attempts == [0, 1]
    held = Flaky(ProviderUncertain("may have billed"))
    with pytest.raises(ProviderUncertain):
        _ask(held, ["brief"], {}, "verdict", sleep=lambda _: None)
    assert held.attempts == [0]
