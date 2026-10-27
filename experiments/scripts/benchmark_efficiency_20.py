"""20-task efficiency comparison: base BasicPolicy vs two-gate scored agent.

All tasks are *clear* local pages with one explicitly specified target, so both
versions must succeed. The point is overhead, not ambiguity handling:

- end-to-end wall time / runner decision & observation time
- screenshots and screenshot bytes
- actor calls, OmniJev calls and token usage
- human requests and simulated human wait (MockHumanProvider delay = 2 s)

Run from cua-gates-pilot (requires the OmniJev Flide server):
    uv run python scripts/benchmark_efficiency_20.py
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass

from nano_cua import Action, BasicPolicy, Finish, Proposal, Runner
from nano_cua_extensions.playwright_browser import PlaywrightIO

from cua_gates_pilot.omnijev import DEFAULT_URL, OmniJevDiagnoseModel
from cua_gates_pilot.policy import GatedPolicy
from cua_gates_pilot.providers import MockHumanProvider

VIEWPORT = (1280, 720)
POS = {"left": (400, 345), "mid": (640, 345), "right": (880, 345)}
HUMAN_DELAY_S = 2.0


def make_html(buttons: list[tuple[str, str]]) -> str:
    positions = ["mid"] if len(buttons) == 1 else (["left", "right"] if len(buttons) == 2 else ["left", "mid", "right"])
    blocks = [
        f'<button id="{button_id}" class="{position}">{label}</button>'
        for (button_id, label), position in zip(buttons, positions)
    ]
    style = """
    html,body{height:100%;margin:0}body{font-family:system-ui;background:#0b1220;color:#e5e7eb}
    h1{position:absolute;top:80px;left:0;right:0;text-align:center}
    button{position:absolute;top:300px;width:200px;height:90px;font-size:24px;border:0;
           border-radius:12px;background:#2563eb;color:#fff;cursor:pointer}
    .left{left:300px}.mid{left:540px}.right{left:780px}
    #status{position:absolute;bottom:60px;left:0;right:0;text-align:center;color:#94a3b8;font-size:22px}
    """
    script = """
    for (const b of document.querySelectorAll('button')) {
      b.addEventListener('click', () => {
        document.getElementById('status').textContent = 'CLICKED ' + b.id.toUpperCase();
      });
    }
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + style
        + "</style></head><body><h1>Choose the correct action</h1>"
        + "".join(blocks)
        + "<p id='status'>Nothing clicked</p><script>"
        + script
        + "</script></body></html>"
    )


@dataclass(frozen=True)
class Task:
    id: str
    task: str
    buttons: tuple[tuple[str, str], ...]
    intended_pos: str
    expected: str


TASKS: tuple[Task, ...] = (
    Task("E01", "Click the Start button.", (("start", "Start"),), "mid", "CLICKED START"),
    Task("E02", "Click the Save button.", (("save", "Save"), ("delete", "Delete")), "left", "CLICKED SAVE"),
    Task("E03", "Click the Decline button.", (("accept", "Accept"), ("decline", "Decline")), "right", "CLICKED DECLINE"),
    Task(
        "E04", "Click the Next button.",
        (("previous", "Previous"), ("next", "Next"), ("finish", "Finish")), "mid", "CLICKED NEXT",
    ),
    Task("E05", "Confirm the booking.", (("confirm", "Confirm"),), "mid", "CLICKED CONFIRM"),
    Task("E06", "Click the Retry button.", (("retry", "Retry"), ("cancel", "Cancel")), "left", "CLICKED RETRY"),
    Task("E07", "Submit the payment.", (("pay", "Pay"),), "mid", "CLICKED PAY"),
    Task("E08", "Click the Close button.", (("open", "Open"), ("close", "Close")), "right", "CLICKED CLOSE"),
    Task(
        "E09", "Choose Green.",
        (("red", "Red"), ("green", "Green"), ("blue", "Blue")), "mid", "CLICKED GREEN",
    ),
    Task("E10", "Click Continue.", (("continue", "Continue"),), "mid", "CLICKED CONTINUE"),
    Task("E11", "Approve the request.", (("approve", "Approve"), ("reject", "Reject")), "left", "CLICKED APPROVE"),
    Task(
        "E12", "Choose High.",
        (("low", "Low"), ("medium", "Medium"), ("high", "High")), "right", "CLICKED HIGH",
    ),
    Task("E13", "Click Download.", (("download", "Download"), ("upload", "Upload")), "left", "CLICKED DOWNLOAD"),
    Task("E14", "Click Finish.", (("finish", "Finish"),), "mid", "CLICKED FINISH"),
    Task("E15", "Click No.", (("yes", "Yes"), ("no", "No")), "right", "CLICKED NO"),
    Task(
        "E16", "Choose Month.",
        (("day", "Day"), ("week", "Week"), ("month", "Month")), "right", "CLICKED MONTH",
    ),
    Task("E17", "Click Previous.", (("previous", "Previous"), ("next", "Next")), "left", "CLICKED PREVIOUS"),
    Task("E18", "Retry the request.", (("retry", "Retry"),), "mid", "CLICKED RETRY"),
    Task("E19", "Click Leave.", (("stay", "Stay"), ("leave", "Leave")), "right", "CLICKED LEAVE"),
    Task("E20", "Click Done.", (("done", "Done"),), "mid", "CLICKED DONE"),
)


def click(pos: str) -> Action:
    x, y = POS[pos]
    return Action(space="browser", kind="click", args={"x": x, "y": y})


class TaskActor:
    def __init__(self, action: Action) -> None:
        self._action = action
        self._normal_used = False
        self._clarified_used = False

    async def propose(self, context: object) -> Proposal:
        if "User clarification:" in getattr(context, "task", ""):
            if not self._clarified_used:
                self._clarified_used = True
                return self._action
            return Finish(reason="clarified action completed")
        if not self._normal_used:
            self._normal_used = True
            return self._action
        return Finish(reason="task actor done")


class CountingActor:
    def __init__(self, inner: TaskActor) -> None:
        self._inner = inner
        self.calls = 0

    async def propose(self, context: object) -> Proposal:
        self.calls += 1
        return await self._inner.propose(context)


class RecordingExecutor:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.actions: list[Action] = []

    async def execute(self, action: Action) -> object:
        self.actions.append(action)
        return await self._inner.execute(action)  # type: ignore[attr-defined]


class RecordingObserver:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.count = 0
        self.bytes = 0

    async def observe(self, request: object) -> object:
        observation = await self._inner.observe(request)  # type: ignore[attr-defined]
        self.count += 1
        payload = observation.payload  # type: ignore[attr-defined]
        if isinstance(payload, bytes):
            self.bytes += len(payload)
        return observation


class TimedHumanProvider:
    def __init__(self, inner: MockHumanProvider) -> None:
        self._inner = inner
        self.requests = 0
        self.wait_s = 0.0

    async def ask(self, question: object) -> object:
        self.requests += 1
        started = time.perf_counter()
        try:
            return await self._inner.ask(question)  # type: ignore[arg-type]
        finally:
            self.wait_s += time.perf_counter() - started


def check_api(url: str) -> None:
    base = url.rsplit("/v1/", 1)[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base}/docs", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"OmniJev /docs returned {response.status}")


async def run_task(group: PlaywrightIO, mode: str, task: Task, url: str) -> dict[str, object]:
    await group.page.set_content(make_html(list(task.buttons)))
    intended = click(task.intended_pos)
    actor = CountingActor(TaskActor(intended))
    observer = RecordingObserver(group)
    executor = RecordingExecutor(group)
    diagnoser: OmniJevDiagnoseModel | None = None
    human: TimedHumanProvider | None = None
    if mode == "gated":
        diagnoser = OmniJevDiagnoseModel(url=url)
        human = TimedHumanProvider(MockHumanProvider(answers=["Mock user: proceed."], delay_s=HUMAN_DELAY_S))
        policy = GatedPolicy(
            actor=actor,
            observe_diagnoser=diagnoser,
            ask_diagnoser=diagnoser,
            human_provider=human,
            observe_gate_enabled=True,
            ask_human_gate_enabled=True,
        )
    else:
        policy = BasicPolicy(actor)

    runner = Runner(policy=policy, observer=observer, executor=executor, task=task.task, max_actions=3, max_observations=6)
    started = time.perf_counter()
    result = await runner.run()
    page_status = (await group.page.inner_text("#status")).strip()
    elapsed = time.perf_counter() - started
    success = result.success and page_status == task.expected and executor.actions == [intended]
    record: dict[str, object] = {
        "task": task.id,
        "mode": mode,
        "success": success,
        "page_status": page_status,
        "wall_ms": round(elapsed * 1000, 1),
        "screenshots": observer.count,
        "screenshot_bytes": observer.bytes,
        "screenshot_capture_ms": round(result.observation_time_s * 1000, 1),
        "decision_ms": round(result.decision_time_s * 1000, 1),
        "execution_ms": round(result.execution_time_s * 1000, 1),
        "actor_calls": actor.calls,
        "human_requests": human.requests if human else 0,
        "human_wait_ms": round(human.wait_s * 1000, 1) if human else 0.0,
        "omnijev_calls": diagnoser.calls if diagnoser else 0,
        "omnijev_input_tokens": diagnoser.usage_input_tokens if diagnoser else 0,
        "omnijev_output_tokens": diagnoser.usage_output_tokens if diagnoser else 0,
        "ask_decisions": [],
        "observe_gate_ms": 0.0,
        "ask_gate_ms": 0.0,
    }
    if isinstance(policy, GatedPolicy):
        record["ask_decisions"] = [
            {"decision": event.decision, "score": None if event.score is None else round(event.score, 3)}
            for event in policy.metrics.ask_gate_events
        ]
        record["observe_gate_ms"] = round(policy.metrics.observe_gate_total_s * 1000, 1)
        record["ask_gate_ms"] = round(policy.metrics.ask_gate_total_s * 1000, 1)
    return record


def _summarize(records: list[dict[str, object]]) -> dict[str, object]:
    def mean(key: str) -> float:
        return round(sum(float(record[key]) for record in records) / len(records), 2)  # type: ignore[arg-type]

    return {
        "tasks": len(records),
        "successes": sum(1 for record in records if record["success"]),
        "mean_wall_ms": mean("wall_ms"),
        "mean_screenshots": mean("screenshots"),
        "mean_screenshot_bytes": mean("screenshot_bytes"),
        "mean_screenshot_capture_ms": mean("screenshot_capture_ms"),
        "mean_decision_ms": mean("decision_ms"),
        "mean_actor_calls": mean("actor_calls"),
        "mean_human_requests": mean("human_requests"),
        "mean_human_wait_ms": mean("human_wait_ms"),
        "mean_omnijev_calls": mean("omnijev_calls"),
        "total_omnijev_input_tokens": sum(int(record["omnijev_input_tokens"]) for record in records),
        "total_omnijev_output_tokens": sum(int(record["omnijev_output_tokens"]) for record in records),
    }


async def main() -> int:
    check_api(DEFAULT_URL)
    group = await PlaywrightIO.launch(headless=True, viewport=VIEWPORT, url=None)
    try:
        baseline = [await run_task(group, "baseline", task, DEFAULT_URL) for task in TASKS]
        gated = [await run_task(group, "gated", task, DEFAULT_URL) for task in TASKS]
    finally:
        await group.close()

    records = baseline + gated
    print("task mode success wall_ms screenshots shot_bytes actor_calls omnijev in_tok out_tok human_reqs human_wait_ms ask")
    for record in records:
        ask = ",".join(f"{event['decision']}:{event['score']}" for event in record["ask_decisions"]) or "-"
        print(
            f"{record['task']:>4} {record['mode']:<8} {str(record['success']):<5} {record['wall_ms']:>8} "
            f"{record['screenshots']:>3} {record['screenshot_bytes']:>9} {record['actor_calls']:>5} "
            f"{record['omnijev_calls']:>6} {record['omnijev_input_tokens']:>6} {record['omnijev_output_tokens']:>7} "
            f"{record['human_requests']:>5} {record['human_wait_ms']:>8} {ask}"
        )
    baseline_summary = _summarize(baseline)
    gated_summary = _summarize(gated)
    summary = {
        "baseline": baseline_summary,
        "gated": gated_summary,
        "delta": {
            "wall_ms": round(gated_summary["mean_wall_ms"] - baseline_summary["mean_wall_ms"], 2),  # type: ignore[operator]
            "screenshots": round(gated_summary["mean_screenshots"] - baseline_summary["mean_screenshots"], 2),  # type: ignore[operator]
            "screenshot_bytes": round(
                gated_summary["mean_screenshot_bytes"] - baseline_summary["mean_screenshot_bytes"], 2  # type: ignore[operator]
            ),
            "actor_calls": round(gated_summary["mean_actor_calls"] - baseline_summary["mean_actor_calls"], 2),  # type: ignore[operator]
            "omnijev_calls": round(gated_summary["mean_omnijev_calls"] - baseline_summary["mean_omnijev_calls"], 2),  # type: ignore[operator]
        },
    }
    print("\nSUMMARY")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
