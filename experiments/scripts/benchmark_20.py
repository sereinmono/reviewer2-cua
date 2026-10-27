"""Small 20-task functional comparison: base BasicPolicy vs two-gate scored agent.

Every task is a local generated page; a deterministic TaskActor proposes the
scripted action. Baseline = base BasicPolicy. Gated = GatedPolicy with the real
local OmniJev/Flide diagnostician (Observe Gate + AskHuman Gate).

Tasks are deliberately split:
- 10 clear tasks: the intended target is specified; both versions should succeed.
- 10 ambiguous tasks: the scripted first candidate is the wrong alternative; the
  gated agent may ask and replan after the scripted human clarification. The
  base BasicPolicy has no clarification channel, so its failures here are a
  functional demonstration, not a fair human-mechanism benchmark.

This is a functional comparison, not a formal benchmark.

Run from cua-gates-pilot (requires the OmniJev Flide server):
    uv run python scripts/benchmark_20.py
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


def make_html(buttons: list[tuple[str, str]]) -> str:
    positions = ["mid"] if len(buttons) == 1 else (["left", "right"] if len(buttons) == 2 else ["left", "mid", "right"])
    blocks = [
        f'<button id="{button_id}" class="{pos}">{label}</button>'
        for (button_id, label), pos in zip(buttons, positions)
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
    kind: str  # clear | ambiguous
    task: str
    buttons: tuple[tuple[str, str], ...]
    intended_pos: str
    wrong_pos: str | None
    expected: str
    human_answer: str


TASKS: tuple[Task, ...] = (
    Task("C1", "clear", "Click the Start button.", (("start", "Start"),), "mid", None, "CLICKED START", ""),
    Task(
        "C2", "clear", "Click the Save button.",
        (("save", "Save"), ("delete", "Delete")), "left", None, "CLICKED SAVE", "",
    ),
    Task(
        "C3", "clear", "Click the Reset button.",
        (("start", "Start"), ("stop", "Stop"), ("reset", "Reset")), "right", None, "CLICKED RESET", "",
    ),
    Task(
        "C4", "clear", "Click the Continue button.",
        (("continue", "Continue"), ("cancel", "Cancel")), "left", None, "CLICKED CONTINUE", "",
    ),
    Task("C5", "clear", "Submit the form.", (("submit", "Submit"),), "mid", None, "CLICKED SUBMIT", ""),
    Task(
        "C6", "clear", "Click the Decline button.",
        (("accept", "Accept"), ("decline", "Decline")), "right", None, "CLICKED DECLINE", "",
    ),
    Task(
        "C7", "clear", "Click the Next button.",
        (("previous", "Previous"), ("next", "Next"), ("finish", "Finish")), "mid", None, "CLICKED NEXT", "",
    ),
    Task("C8", "clear", "Confirm the booking.", (("confirm", "Confirm"),), "mid", None, "CLICKED CONFIRM", ""),
    Task(
        "C9", "clear", "Click the Retry button.",
        (("retry", "Retry"), ("cancel", "Cancel")), "left", None, "CLICKED RETRY", "",
    ),
    Task("C10", "clear", "Submit the payment.", (("pay", "Pay"),), "mid", None, "CLICKED PAY", ""),
    Task(
        "A1", "ambiguous", "Close the document.",
        (("save", "Save"), ("discard", "Discard")), "right", "left", "CLICKED DISCARD",
        "Mock user: discard the changes.",
    ),
    Task(
        "A2", "ambiguous", "Finish the form.",
        (("submit", "Submit"), ("cancel", "Cancel")), "right", "left", "CLICKED CANCEL",
        "Mock user: cancel the form.",
    ),
    Task(
        "A3", "ambiguous", "Submit the form.",
        (("draft", "Submit draft"), ("submit_final", "Submit final")), "right", "left", "CLICKED SUBMIT_FINAL",
        "Mock user: submit the final version.",
    ),
    Task(
        "A4", "ambiguous", "Delete.",
        (("delete_all", "Delete all"), ("delete_selected", "Delete selected")), "right", "left", "CLICKED DELETE_SELECTED",
        "Mock user: delete only the selected items.",
    ),
    Task(
        "A5", "ambiguous", "Send the report.",
        (("send_now", "Send now"), ("schedule", "Schedule")), "right", "left", "CLICKED SCHEDULE",
        "Mock user: schedule it for tomorrow.",
    ),
    Task(
        "A6", "ambiguous", "Handle the email.",
        (("send_email", "Send email"), ("save_draft", "Save draft")), "right", "left", "CLICKED SAVE_DRAFT",
        "Mock user: save it as a draft.",
    ),
    Task(
        "A7", "ambiguous", "Clean up the message.",
        (("archive", "Archive"), ("delete", "Delete")), "right", "left", "CLICKED DELETE",
        "Mock user: delete it.",
    ),
    Task(
        "A8", "ambiguous", "Review the request.",
        (("approve", "Approve"), ("reject", "Reject")), "right", "left", "CLICKED REJECT",
        "Mock user: reject the request.",
    ),
    Task(
        "A9", "ambiguous", "Integrate the changes.",
        (("merge", "Merge"), ("rebase", "Rebase")), "right", "left", "CLICKED REBASE",
        "Mock user: rebase onto main.",
    ),
    Task(
        "A10", "ambiguous", "Access the file.",
        (("download", "Download"), ("open_browser", "Open in browser")), "right", "left", "CLICKED OPEN_BROWSER",
        "Mock user: open it in the browser.",
    ),
)


def click(pos: str) -> Action:
    x, y = POS[pos]
    return Action(space="browser", kind="click", args={"x": x, "y": y})


class TaskActor:
    """Normal script, plus one replan proposal if a human clarification appears."""

    def __init__(self, normal: list[Proposal], clarified: Proposal | None) -> None:
        self._normal = normal
        self._clarified = clarified
        self._index = 0
        self._clarified_used = False

    async def propose(self, context: object) -> Proposal:
        if "User clarification:" in getattr(context, "task", ""):
            if not self._clarified_used and self._clarified is not None:
                self._clarified_used = True
                return self._clarified
            return Finish(reason="clarified action completed")
        if self._index < len(self._normal):
            proposal = self._normal[self._index]
            self._index += 1
            return proposal
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


def check_api(url: str) -> None:
    base = url.rsplit("/v1/", 1)[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base}/docs", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"OmniJev /docs returned {response.status}")


def _event(event: object) -> dict[str, object]:
    return {
        "decision": event.decision,  # type: ignore[attr-defined]
        "score": None if event.score is None else round(event.score, 3),  # type: ignore[attr-defined]
        "signals": {key: round(value, 3) for key, value in dict(event.signals).items()},  # type: ignore[attr-defined]
        "latency_ms": round(event.latency_ms, 1),  # type: ignore[attr-defined]
        "error": event.error,  # type: ignore[attr-defined]
    }


async def run_task(group: PlaywrightIO, mode: str, task: Task, url: str) -> dict[str, object]:
    await group.page.set_content(make_html(list(task.buttons)))
    intended = click(task.intended_pos)
    if mode == "gated":
        if task.kind == "ambiguous":
            assert task.wrong_pos is not None
            initial = click(task.wrong_pos)
            clarified = intended
        else:
            initial = intended
            clarified = intended
        actor = CountingActor(TaskActor([initial, Finish(reason="task actor done")], clarified))
        diagnoser = OmniJevDiagnoseModel(url=url)
        human = MockHumanProvider(answers=[task.human_answer] if task.human_answer else ["Mock user: proceed."])
        policy = GatedPolicy(
            actor=actor,
            observe_diagnoser=diagnoser,
            ask_diagnoser=diagnoser,
            human_provider=human,
            observe_gate_enabled=True,
            ask_human_gate_enabled=True,
        )
    else:
        initial = intended if task.kind == "clear" else click(task.wrong_pos)  # type: ignore[arg-type]
        actor = CountingActor(TaskActor([initial, Finish(reason="task actor done")], None))
        diagnoser = None
        human = None
        policy = BasicPolicy(actor)

    executor = RecordingExecutor(group)
    runner = Runner(policy=policy, observer=group, executor=executor, task=task.task, max_actions=3, max_observations=6)
    started = time.perf_counter()
    result = await runner.run()
    page_status = (await group.page.inner_text("#status")).strip()
    elapsed = time.perf_counter() - started
    success = result.success and page_status == task.expected and executor.actions == [intended]
    record: dict[str, object] = {
        "task": task.id,
        "kind": task.kind,
        "mode": mode,
        "success": success,
        "page_status": page_status,
        "expected": task.expected,
        "screenshots": result.observation_count,
        "executed": [action.args for action in executor.actions],
        "human_requests": 0,
        "actor_calls": actor.calls,
        "omnijev_calls": 0,
        "ask_gate": [],
        "observe_gate": [],
        "elapsed_ms": round(elapsed * 1000, 1),
    }
    if isinstance(policy, GatedPolicy):
        record["human_requests"] = len(human.questions) if human else 0
        record["omnijev_calls"] = diagnoser.calls if diagnoser else 0
        record["ask_gate"] = [_event(event) for event in policy.metrics.ask_gate_events]
        record["observe_gate"] = [_event(event) for event in policy.metrics.observe_gate_events]
    return record


def summarize(records: list[dict[str, object]], mode: str) -> dict[str, object]:
    selected = [record for record in records if record["mode"] == mode]
    def mean(key: str) -> float:
        return round(sum(float(record[key]) for record in selected) / len(selected), 3)  # type: ignore[arg-type]

    return {
        "mode": mode,
        "tasks": len(selected),
        "successes": sum(1 for record in selected if record["success"]),
        "clear_successes": sum(1 for record in selected if record["kind"] == "clear" and record["success"]),
        "ambiguous_successes": sum(1 for record in selected if record["kind"] == "ambiguous" and record["success"]),
        "mean_screenshots": mean("screenshots"),
        "total_human_requests": sum(int(record["human_requests"]) for record in selected),
        "mean_actor_calls": mean("actor_calls"),
        "mean_omnijev_calls": mean("omnijev_calls"),
        "mean_elapsed_ms": mean("elapsed_ms"),
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
    print("task kind mode success status screenshots ask_decisions human_reqs elapsed_ms")
    for record in records:
        ask = ",".join(f"{event['decision']}:{event['score']}" for event in record["ask_gate"]) or "-"
        print(
            f"{record['task']:>4} {record['kind']:<9} {record['mode']:<8} "
            f"{str(record['success']):<5} {record['page_status']:<22} {record['screenshots']:>3} "
            f"{ask:<22} {record['human_requests']:>3} {record['elapsed_ms']:>9}"
        )
    baseline_summary = summarize(records, "baseline")
    gated_summary = summarize(records, "gated")
    print("\nSUMMARY")
    print(json.dumps({"baseline": baseline_summary, "gated": gated_summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
