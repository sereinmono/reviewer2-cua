"""Playwright checks for the two-gate agent with the real local OmniJev model.

Scenarios:
1. clear page: one obvious centre button -> AskHuman Gate must PASS (no human).
2. ambiguous page: task "Close the document" with Save / Discard buttons ->
   AskHuman Gate must ASK; the original candidate must not execute, the human
   clarification must lead to a replanned click on Discard.

Run from cua-gates-pilot (requires the OmniJev Flide server from
D:\\Research26fall\\omnijev-deploy to be running):
    uv run python scripts/omnijev_playwright_check.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import nano_cua_extensions
from nano_cua import Action, BasicPolicy, Finish, MockActor, Proposal, Runner
from nano_cua_extensions.playwright_browser import PlaywrightIO

from cua_gates_pilot.omnijev import DEFAULT_URL, OmniJevDiagnoseModel
from cua_gates_pilot.policy import GatedPolicy
from cua_gates_pilot.providers import MockHumanProvider

DEMO_PAGE = Path(nano_cua_extensions.__file__).parent / "playwright_browser" / "demo_page.html"
VIEWPORT = (1280, 720)
AMBIGUOUS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ambiguous choices</title>
<style>
  html, body { height: 100%; margin: 0; }
  body { font-family: system-ui, sans-serif; background: #0b1220; color: #e5e7eb; }
  h1 { position: absolute; top: 80px; left: 0; right: 0; text-align: center; }
  button { position: absolute; top: 300px; width: 220px; height: 90px; font-size: 26px;
           border: 0; border-radius: 12px; background: #2563eb; color: #fff; cursor: pointer; }
  #left { left: 300px; } #right { left: 760px; }
  #status { position: absolute; bottom: 60px; left: 0; right: 0; text-align: center; font-size: 22px; color: #94a3b8; }
</style></head>
<body>
  <h1>Choose the correct action</h1>
  <button id="left">Save</button>
  <button id="right">Discard</button>
  <p id="status">Nothing clicked</p>
  <script>
    for (const b of document.querySelectorAll('button')) {
      b.addEventListener('click', () => {
        document.getElementById('status').textContent = 'CLICKED ' + b.id.toUpperCase();
        document.title = 'clicked-' + b.id;
      });
    }
  </script>
</body></html>"""

LEFT_CLICK = Action(space="browser", kind="click", args={"x": 410, "y": 345})
RIGHT_CLICK = Action(space="browser", kind="click", args={"x": 870, "y": 345})
CLEAR_CLICK = Action(space="browser", kind="click", args={"x": 640, "y": 360})
CLEAR_TASK = "Click the big button in the centre of the page, then finish."


class CountingActor:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    async def propose(self, context: Any) -> Proposal:
        self.calls += 1
        return await self._inner.propose(context)


class RecordingExecutor:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.actions: list[Action] = []

    async def execute(self, action: Action) -> Any:
        self.actions.append(action)
        return await self._inner.execute(action)


def check_api(url: str) -> None:
    base = url.rsplit("/v1/", 1)[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base}/docs", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"OmniJev /docs returned {response.status}")


def _event(event: Any) -> dict[str, Any]:
    return {
        "step": event.step,
        "decision": event.decision,
        "score": None if event.score is None else round(event.score, 4),
        "reason": event.reason,
        "signals": {key: round(value, 4) for key, value in dict(event.signals).items()},
        "latency_ms": round(event.latency_ms, 1),
        "error": event.error,
    }


async def run_gated(
    *,
    page_url: str,
    task: str,
    proposals: list[Proposal],
    human_answers: list[str],
    url: str,
) -> dict[str, Any]:
    group = await PlaywrightIO.launch(headless=True, viewport=VIEWPORT, url=page_url)
    actor = CountingActor(MockActor(proposals))
    executor = RecordingExecutor(group)
    diagnoser = OmniJevDiagnoseModel(url=url)
    human = MockHumanProvider(answers=human_answers)
    policy = GatedPolicy(
        actor=actor,
        observe_diagnoser=diagnoser,
        ask_diagnoser=diagnoser,
        human_provider=human,
        observe_gate_enabled=True,
        ask_human_gate_enabled=True,
    )
    runner = Runner(policy=policy, observer=group, executor=executor, task=task, max_actions=4, max_observations=6)
    started = time.perf_counter()
    try:
        result = await runner.run()
        page_status = (await group.page.inner_text("#status")).strip()
    finally:
        wall_s = time.perf_counter() - started
        await group.close()

    metrics = policy.metrics
    return {
        "success": result.success,
        "reason": result.reason,
        "page_status": page_status,
        "screenshots": result.observation_count,
        "main_actor_calls": actor.calls,
        "executed_actions": [{"space": a.space, "kind": a.kind, "args": dict(a.args)} for a in executor.actions],
        "human_requests": len(human.questions),
        "human_replans": metrics.human_replans,
        "omnijev_calls": diagnoser.calls,
        "omnijev_model": diagnoser.model_name,
        "observe_gate_events": [_event(e) for e in metrics.observe_gate_events],
        "ask_gate_events": [_event(e) for e in metrics.ask_gate_events],
        "timings_ms": {
            "end_to_end": round(wall_s * 1000, 1),
            "observe_gate": round(metrics.observe_gate_total_s * 1000, 1),
            "ask_gate": round(metrics.ask_gate_total_s * 1000, 1),
        },
    }


async def run_baseline_clear() -> dict[str, Any]:
    group = await PlaywrightIO.launch(headless=True, viewport=VIEWPORT, url=DEMO_PAGE.resolve().as_uri())
    actor = CountingActor(MockActor([CLEAR_CLICK, Finish(reason="mock: clicked centre button")]))
    executor = RecordingExecutor(group)
    runner = Runner(policy=BasicPolicy(actor), observer=group, executor=executor, task=CLEAR_TASK, max_actions=4, max_observations=6)
    started = time.perf_counter()
    try:
        result = await runner.run()
        page_status = (await group.page.inner_text("#status")).strip()
    finally:
        wall_s = time.perf_counter() - started
        await group.close()
    return {
        "success": result.success,
        "page_status": page_status,
        "screenshots": result.observation_count,
        "main_actor_calls": actor.calls,
        "executed_actions": [{"kind": a.kind, "args": dict(a.args)} for a in executor.actions],
        "end_to_end_ms": round(wall_s * 1000, 1),
    }


async def main() -> int:
    url = DEFAULT_URL
    check_api(url)
    print(f"OmniJev server OK: {url}")

    clear_gated = await run_gated(
        page_url=DEMO_PAGE.resolve().as_uri(),
        task=CLEAR_TASK,
        proposals=[CLEAR_CLICK, Finish(reason="mock: clicked centre button")],
        human_answers=["Mock user: proceed with the obvious button."],
        url=url,
    )
    baseline_clear = await run_baseline_clear()

    with tempfile.TemporaryDirectory(prefix="cua-gates-ambiguous-") as tmp:
        page_path = Path(tmp) / "ambiguous.html"
        page_path.write_text(AMBIGUOUS_HTML, encoding="utf-8")
        ambiguous = await run_gated(
            page_url=page_path.resolve().as_uri(),
            task="Close the document, then finish.",
            proposals=[LEFT_CLICK, RIGHT_CLICK, Finish(reason="mock: discarded via right button")],
            human_answers=["Mock user: discard the changes, do not save."],
            url=url,
        )

    report = {"baseline_clear": baseline_clear, "gated_clear": clear_gated, "gated_ambiguous": ambiguous}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    clear_ok = (
        clear_gated["success"]
        and clear_gated["page_status"] == "CLICKED 1 at (640, 360)"
        and clear_gated["human_requests"] == 0
        and len(clear_gated["executed_actions"]) == 1
        and clear_gated["omnijev_calls"] >= 2
    )
    ambiguous_ok = (
        ambiguous["success"]
        and ambiguous["page_status"] == "CLICKED RIGHT"
        and ambiguous["human_requests"] == 1
        and ambiguous["human_replans"] == 1
        and ambiguous["executed_actions"] == [{"space": "browser", "kind": "click", "args": {"x": 870, "y": 345}}]
        and any(event["decision"] == "ASK" for event in ambiguous["ask_gate_events"])
    )
    print(f"\nCLEAR CHECK     : {'PASS' if clear_ok else 'FAIL'} (expected no human request)")
    print(f"AMBIGUOUS CHECK : {'PASS' if ambiguous_ok else 'FAIL'} (expected ASK + replanned right click)")
    return 0 if (clear_ok and ambiguous_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
