"""Fixed gate-function tests (pure, no I/O, no model)."""

from __future__ import annotations

import pytest

from cua_gates_pilot.gates import (
    HUMAN_THRESHOLD,
    GateDecision,
    GateSignalError,
    conservative_human_decision,
    conservative_observe_decision,
    decide_human,
    decide_observe,
    human_score,
    observe_score,
)


def test_observe_score_matches_fixed_formula():
    assert observe_score({"info_missing": 1.0, "possibly_stale": 0.0}) == pytest.approx(0.6)
    assert observe_score({"info_missing": 0.0, "possibly_stale": 1.0}) == pytest.approx(0.4)
    assert observe_score({"info_missing": 0.5, "possibly_stale": 0.5}) == pytest.approx(0.5)


def test_observe_threshold_is_inclusive():
    at_threshold = {"info_missing": 1.0, "possibly_stale": 0.0}
    decision = decide_observe(at_threshold)
    assert isinstance(decision, GateDecision)
    assert decision.decision == "OBSERVE"
    assert decision.gate == "observe"
    assert decision.score == pytest.approx(0.6)
    assert decision.error is None

    below = {"info_missing": 0.9, "possibly_stale": 0.0}
    assert decide_observe(below).decision == "REUSE"


def test_human_threshold_is_inclusive():
    assert decide_human({"intent_ambiguity": HUMAN_THRESHOLD}).decision == "ASK"
    assert decide_human({"intent_ambiguity": 0.64}).decision == "PASS"
    assert human_score({"intent_ambiguity": 0.7}) == pytest.approx(0.7)


def test_signals_are_clamped_to_unit_interval():
    assert observe_score({"info_missing": 5.0, "possibly_stale": -2.0}) == pytest.approx(0.6)
    assert human_score({"intent_ambiguity": 2.0}) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "signals",
    [
        {},
        {"info_missing": 0.5},
        {"info_missing": "high", "possibly_stale": 0.2},
        {"info_missing": True, "possibly_stale": 0.2},
        {"info_missing": float("nan"), "possibly_stale": 0.2},
        None,
        [("info_missing", 0.5)],
    ],
)
def test_missing_or_invalid_signals_raise(signals):
    with pytest.raises(GateSignalError):
        observe_score(signals)


def test_conservative_decisions_are_explicit():
    observe = conservative_observe_decision("timeout")
    assert observe.decision == "OBSERVE" and "conservative" in observe.reason and observe.error == "timeout"

    human = conservative_human_decision("boom")
    assert human.decision == "ASK" and "conservative" in human.reason and human.error == "boom"


def test_gate_decision_is_frozen():
    decision = decide_observe({"info_missing": 0.9, "possibly_stale": 0.9})
    with pytest.raises(Exception):
        decision.decision = "REUSE"  # type: ignore[misc]
