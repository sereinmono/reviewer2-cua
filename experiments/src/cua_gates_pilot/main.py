"""Composition root for the two-gate pilot.

Runs the same local-page task in one of two modes:

* ``baseline`` -- the base ``BasicPolicy`` (fixed observe-after-every-action);
* ``gated``    -- this package's ``GatedPolicy`` with independently switchable
                  Observe Gate and AskHuman Gate.

The pilot core (gates/providers/policy) has no browser or VLM dependencies; the
Playwright group is imported lazily from the external ``nano-cua-extensions``
package only for these demos. No real OmniJev / VLM API is used here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nano_cua import Action, BasicPolicy, Finish, MockActor, Proposal, Runner

from .gates import GateDecision
from .policy import GatedPolicy
from .providers import MockHumanProvider, MockOmniJev

DEFAULT_VIEWPORT = (1280, 720)


def _click(x: int, y: int) -> Action:
    return Action(space="browser", kind="click", args={"x": x, "y": y})


@dataclass(frozen=True)
class Scenario:
    name: str
    task: str
    script: tuple[Proposal, ...]
    observe_signals: tuple[dict[str, float], ...]
    human_signals: tuple[dict[str, float], ...]
    human_answers: tuple[str | None, ...]


SCENARIOS: dict[str, Scenario] = {
    "reuse": Scenario(
        name="reuse",
        task="Click the big button in the centre of the page, then finish.",
        script=(_click(640, 360), Finish(reason="mock: clicked the centre button")),
        observe_signals=({"info_missing": 0.1, "possibly_stale": 0.1},),
        human_signals=({"intent_ambiguity": 0.1},),
        human_answers=(),
    ),
    "observe": Scenario(
        name="observe",
        task="Click the big button in the centre of the page, then finish.",
        script=(_click(640, 360), Finish(reason="mock: clicked the centre button")),
        observe_signals=({"info_missing": 1.0, "possibly_stale": 1.0},),
        human_signals=({"intent_ambiguity": 0.1},),
        human_answers=(),
    ),
    "ask": Scenario(
        name="ask",
        task="Click the big button in the centre of the page, then finish.",
        # First candidate is deliberately wrong; the AskHuman gate must stop it.
        script=(
            _click(30, 30),
            _click(640, 360),
            Finish(reason="mock: clicked the centre button after clarification"),
        ),
        observe_signals=({"info_missing": 0.1, "possibly_stale": 0.1}, {"info_missing": 0.1, "possibly_stale": 0.1}),
        human_signals=({"intent_ambiguity": 1.0},),
        human_answers=("Mock user: click the large button in the centre of the page.",),
    ),
}


class CountingActor:
    """Demo-only wrapper: counts main-actor calls and latency without editing it."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0
        self.total_s = 0.0

    async def propose(self, context: Any) -> Proposal:
        started = time.perf_counter()
        try:
            return await self._inner.propose(context)
        finally:
            self.calls += 1
            self.total_s += time.perf_counter() - started


class CountingObserver:
    """Demo-only wrapper: proves how many screenshots the base actually requested."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    async def observe(self, request: Any) -> Any:
        self.calls += 1
        return await self._inner.observe(request)


class RecordingExecutor:
    """Demo-only wrapper: records actions/results the base executor actually ran."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.actions: list[Action] = []
        self.errors: list[str | None] = []

    async def execute(self, action: Action) -> Any:
        result = await self._inner.execute(action)
        self.actions.append(action)
        self.errors.append(result.error)
        return result


def _load_external_browser_group():
    """Lazily import the external Playwright compatibility group (demo only)."""
    try:
        import nano_cua_extensions
        from nano_cua_extensions.playwright_browser import PlaywrightIO
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "browser demo needs the external nano-cua-extensions package; run `uv sync` "
            "in cua-gates-pilot and `uv run playwright install chromium`"
        ) from exc
    demo_page = Path(nano_cua_extensions.__file__).parent / "playwright_browser" / "demo_page.html"
    return PlaywrightIO, demo_page


def _event_dict(decision: GateDecision) -> dict[str, Any]:
    return {
        "step": decision.step,
        "gate": decision.gate,
        "decision": decision.decision,
        "score": None if decision.score is None else round(decision.score, 4),
        "reason": decision.reason,
        "signals": dict(decision.signals),
        "error": decision.error,
        "latency_ms": round(decision.latency_ms, 3),
    }


async def _page_status(group: Any) -> str | None:
    page = getattr(group, "page", None)
    if page is None:
        return None
    try:
        return (await page.inner_text("#status")).strip()
    except Exception:  # noqa: BLE001 - demo-only visibility, never fails the run
        return None


async def run_browser_demo(args: argparse.Namespace) -> int:
    PlaywrightIO, demo_page = _load_external_browser_group()
    scenario = SCENARIOS[args.scenario]
    group = await PlaywrightIO.launch(
        headless=not args.headed,
        viewport=DEFAULT_VIEWPORT,
        url=demo_page.resolve().as_uri(),
    )
    observer = CountingObserver(group)
    executor = RecordingExecutor(group)
    actor = CountingActor(MockActor(list(scenario.script)))
    observe_jev = MockOmniJev(observe_responses=list(scenario.observe_signals))
    ask_jev = MockOmniJev(human_responses=list(scenario.human_signals))
    human = MockHumanProvider(answers=list(scenario.human_answers))
    gated = args.mode == "gated"

    policy = (
        GatedPolicy(
            actor=actor,
            observe_diagnoser=observe_jev,
            ask_diagnoser=ask_jev,
            human_provider=human,
            observe_gate_enabled=args.observe_gate == "on",
            ask_human_gate_enabled=args.ask_gate == "on",
            human_timeout_s=args.human_timeout_s,
        )
        if gated
        else BasicPolicy(actor)
    )
    runner = Runner(
        policy=policy,
        observer=observer,
        executor=executor,
        task=scenario.task,
        max_actions=args.max_actions,
        max_observations=args.max_observations,
    )
    started = time.perf_counter()
    page_status: str | None = None
    try:
        result = await runner.run()
        page_status = await _page_status(group)
    finally:
        wall_s = time.perf_counter() - started
        await group.close()

    summary: dict[str, Any] = {
        "mode": args.mode,
        "scenario": scenario.name,
        "task": scenario.task,
        "page_status": page_status,
        "success": result.success,
        "reason": result.reason,
        "screenshots": observer.calls,
        "runner_observation_commands": result.observation_count,
        "executed_actions": result.action_count,
        "executed_action_details": [
            {"space": action.space, "kind": action.kind, "args": dict(action.args)} for action in executor.actions
        ],
        "execution_errors": executor.errors,
        "main_vlm_calls": actor.calls,
        "human_requests": len(human.questions),
        "timings_ms": {
            "end_to_end": round(wall_s * 1000, 2),
            "runner_total": round(result.total_time_s * 1000, 2),
            "actor": round(actor.total_s * 1000, 2),
        },
    }
    if gated:
        metrics = policy.metrics
        summary["gates_enabled"] = {
            "observe": args.observe_gate == "on",
            "ask_human": args.ask_gate == "on",
        }
        summary["gate_events"] = {
            "observe": [_event_dict(event) for event in metrics.observe_gate_events],
            "ask_human": [_event_dict(event) for event in metrics.ask_gate_events],
        }
        summary["timings_ms"].update(
            {
                "observe_gate": round(metrics.observe_gate_total_s * 1000, 2),
                "ask_gate": round(metrics.ask_gate_total_s * 1000, 2),
            }
        )
        summary["human_replans"] = metrics.human_replans
        summary["policy_main_vlm_calls"] = metrics.main_vlm_calls
    else:
        summary["gates_enabled"] = None  # baseline uses the base BasicPolicy

    if summary["screenshots"] != summary["runner_observation_commands"]:
        summary["warning"] = "screenshot counter mismatch (should not happen)"

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cua-gates-pilot",
        description="External two-gate pilot for nano-cua (baseline / gated local-page demos).",
    )
    parser.add_argument("--mode", choices=("baseline", "gated"), default="gated")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="reuse")
    parser.add_argument("--observe-gate", choices=("on", "off"), default="on")
    parser.add_argument("--ask-gate", choices=("on", "off"), default="on")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--max-actions", type=int, default=6)
    parser.add_argument("--max-observations", type=int, default=8)
    parser.add_argument("--human-timeout-s", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_browser_demo(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI reports a clean failure
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
