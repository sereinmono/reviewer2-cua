"""Fixed, non-neural gate decisions over frozen diagnostic signals.

The diagnostic model (OmniJev or a mock) outputs normalized signals in
``[0, 1]``; this module only applies the pre-registered fixed formulas and
thresholds. No I/O, no model calls, no learning.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

OBSERVE_INFO_WEIGHT = 0.6
OBSERVE_STALE_WEIGHT = 0.4
OBSERVE_THRESHOLD = 0.60

HUMAN_THRESHOLD = 0.65


class GateSignalError(ValueError):
    """Raised when a diagnostic signal is missing or not a usable number."""


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One gate evaluation: decision, fixed-formula score, reason and timing."""

    gate: str  # "observe" | "ask_human"
    decision: str  # "REUSE" | "OBSERVE" | "PASS" | "ASK"
    reason: str
    score: float | None = None
    signals: Mapping[str, float] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    step: int = 0


def _unit_interval(signals: Mapping[str, float], key: str) -> float:
    if not isinstance(signals, Mapping):
        raise GateSignalError(f"diagnostic signals must be a mapping, got {type(signals).__name__}")
    if key not in signals:
        raise GateSignalError(f"missing diagnostic signal {key!r}")
    raw = signals[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise GateSignalError(f"diagnostic signal {key!r} must be a number, got {type(raw).__name__}")
    value = float(raw)
    if not math.isfinite(value):
        raise GateSignalError(f"diagnostic signal {key!r} must be finite, got {raw!r}")
    return min(max(value, 0.0), 1.0)


def observe_score(signals: Mapping[str, float]) -> float:
    """0.6 * info_missing + 0.4 * possibly_stale (fixed, pre-registered)."""
    info_missing = _unit_interval(signals, "info_missing")
    possibly_stale = _unit_interval(signals, "possibly_stale")
    return OBSERVE_INFO_WEIGHT * info_missing + OBSERVE_STALE_WEIGHT * possibly_stale


def human_score(signals: Mapping[str, float]) -> float:
    """intent_ambiguity (fixed, pre-registered)."""
    return _unit_interval(signals, "intent_ambiguity")


def decide_observe(signals: Mapping[str, float]) -> GateDecision:
    score = observe_score(signals)
    decision = "OBSERVE" if score >= OBSERVE_THRESHOLD else "REUSE"
    reason = f"observe_score={score:.3f} threshold={OBSERVE_THRESHOLD:.2f}"
    return GateDecision(gate="observe", decision=decision, reason=reason, score=score, signals=dict(signals))


def decide_human(signals: Mapping[str, float]) -> GateDecision:
    score = human_score(signals)
    decision = "ASK" if score >= HUMAN_THRESHOLD else "PASS"
    reason = f"intent_ambiguity={score:.3f} threshold={HUMAN_THRESHOLD:.2f}"
    return GateDecision(gate="ask_human", decision=decision, reason=reason, score=score, signals=dict(signals))


def conservative_observe_decision(error: object) -> GateDecision:
    """On diagnostic failure the Observe Gate refreshes once (never acts blind)."""
    detail = str(error)
    return GateDecision(
        gate="observe",
        decision="OBSERVE",
        reason="conservative: observe diagnostic failed, request refresh",
        error=detail,
    )


def conservative_human_decision(error: object) -> GateDecision:
    """On diagnostic failure the AskHuman Gate asks instead of silently passing."""
    detail = str(error)
    return GateDecision(
        gate="ask_human",
        decision="ASK",
        reason="conservative: human diagnostic failed, request clarification",
        error=detail,
    )
