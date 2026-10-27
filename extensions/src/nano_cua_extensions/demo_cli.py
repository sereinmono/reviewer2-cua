"""Demo composition root for the companion Playwright/VLM group.

This lives outside the nano-cua base: it wires the Playwright browser group and
the generic VLM actor into the base Runner. Defaults need no model API key (the
scripted MockActor clicks the bundled local page). Set ``NANO_CUA_API_BASE`` /
``NANO_CUA_MODEL`` (and optionally ``NANO_CUA_API_KEY``) or pass ``--actor vlm``
to use a real VLM.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path

from nano_cua import Action, Actor, BasicPolicy, Finish, MockActor, RunResult, Runner

from .playwright_browser.browser_space import BROWSER_SYSTEM_PROMPT, SPACE, browser_proposal_parser
from .playwright_browser.playwright_io import PlaywrightIO
from .vlm.actor import VLMActor

DEMO_PAGE = Path(__file__).parent / "playwright_browser" / "demo_page.html"
DEFAULT_TASK = "Click the big button in the centre of the page, then finish."
DEFAULT_VIEWPORT = (1280, 720)

_ENV_API_BASE = "NANO_CUA_API_BASE"
_ENV_API_KEY = "NANO_CUA_API_KEY"
_ENV_MODEL = "NANO_CUA_MODEL"
_ENV_CHANNEL = "NANO_CUA_BROWSER_CHANNEL"

_MISSING_BROWSER_HINTS = ("Executable doesn't exist", "is not found", "playwright install")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nano-cua-demo",
        description="Companion demo: nano-cua base + Playwright browser group + mock/VLM actor.",
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help="task description handed to the actor")
    parser.add_argument("--url", default=None, help="page to open (default: bundled demo page)")
    parser.add_argument(
        "--actor",
        choices=("auto", "mock", "vlm"),
        default="auto",
        help="auto: vlm when NANO_CUA_API_BASE + NANO_CUA_MODEL are set, else mock",
    )
    parser.add_argument("--api-base", default=None, help=f"OpenAI-compatible API base (env {_ENV_API_BASE})")
    parser.add_argument("--model", default=None, help=f"model name for the VLM actor (env {_ENV_MODEL})")
    parser.add_argument("--json-mode", action="store_true", help="send response_format=json_object to the VLM")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--browser-channel", default=os.environ.get(_ENV_CHANNEL) or None, help="e.g. msedge, chrome")
    parser.add_argument("--width", type=int, default=DEFAULT_VIEWPORT[0], help="viewport width in pixels")
    parser.add_argument("--height", type=int, default=DEFAULT_VIEWPORT[1], help="viewport height in pixels")
    parser.add_argument("--max-steps", type=int, default=8, help="maximum executed actions")
    parser.add_argument("--max-observations", type=int, default=8, help="maximum observations")
    parser.add_argument("--save-screenshot", default=None, help="write the final observation to this PNG path (opt-in)")
    return parser


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _demo_script(args: argparse.Namespace) -> list[Action | Finish]:
    x, y = args.width // 2, args.height // 2
    return [
        Action(space=SPACE, kind="click", args={"x": x, "y": y}),
        Finish(reason=f"mock: clicked the centre button at ({x}, {y})", success=True),
    ]


def _resolve_actor(args: argparse.Namespace) -> tuple[Actor, str]:
    api_base = (args.api_base or _env(_ENV_API_BASE)).strip()
    model = (args.model or _env(_ENV_MODEL)).strip()
    choice = args.actor
    if choice == "auto":
        if bool(api_base) != bool(model):
            print(
                f"note: only one of API base/model is configured; using mock actor "
                f"(set both {_ENV_API_BASE} and {_ENV_MODEL} for the VLM)",
                file=sys.stderr,
            )
        choice = "vlm" if (api_base and model) else "mock"
    if choice == "mock":
        script = _demo_script(args)
        return MockActor(script), f"mock (scripted, {len(script)} proposals, no API key)"
    if not api_base or not model:
        raise ValueError(
            f"--actor vlm needs an API base and model "
            f"(--api-base/--model or env {_ENV_API_BASE}/{_ENV_MODEL})"
        )
    actor = VLMActor(
        base_url=api_base,
        model=model,
        api_key=_env(_ENV_API_KEY) or None,
        json_mode=args.json_mode,
        system_prompt=BROWSER_SYSTEM_PROMPT,
        proposal_parser=browser_proposal_parser,
    )
    label = f"vlm model={model!r} endpoint={actor.endpoint}"
    if actor.proxy_env_ignored:
        label += " (env proxy vars ignored: unparsable)"
    return actor, label


def _channel_candidates(explicit: str | None) -> list[str | None]:
    if explicit:
        return [explicit]
    return [None, "msedge", "chrome"]


async def _launch_io(args: argparse.Namespace) -> PlaywrightIO:
    url = args.url or DEMO_PAGE.resolve().as_uri()
    last_error: Exception | None = None
    for channel in _channel_candidates(args.browser_channel):
        try:
            io = await PlaywrightIO.launch(
                headless=not args.headed,
                viewport=(args.width, args.height),
                url=url,
                browser_channel=channel,
            )
            if channel is not None and args.browser_channel is None:
                print(f"note: bundled chromium unavailable, using installed browser channel={channel!r}")
            return io
        except Exception as exc:  # noqa: BLE001 - retry only for a missing browser binary
            last_error = exc
            if args.browser_channel is not None or not any(hint in str(exc) for hint in _MISSING_BROWSER_HINTS):
                raise
    raise RuntimeError(
        f"no usable chromium browser found; install one with `uv run playwright install chromium` ({last_error})"
    ) from last_error


def _print_header(args: argparse.Namespace, io: PlaywrightIO, actor_label: str) -> None:
    print("nano-cua demo - companion Playwright group")
    print(f"  task   : {args.task}")
    print(f"  page   : {io.page.url}")
    print(f"  actor  : {actor_label}")
    print(f"  limits : max_actions={args.max_steps}, max_observations={args.max_observations}")


def _print_result(result: RunResult) -> None:
    print("\nsteps:")
    print(f"  {'#':>2}  {'command':<8} {'decision_ms':>11} {'work_ms':>9}  detail")
    for step in result.steps:
        suffix = f"  [failed: {step.error}]" if step.success is False and step.error else ""
        print(
            f"  {step.index:>2}  {step.command:<8} "
            f"{step.decision_s * 1000:>11.2f} {step.work_s * 1000:>9.2f}  {step.detail}{suffix}"
        )
    status = "SUCCESS" if result.success else "FAILURE"
    print(f"\nresult : {status} - {result.reason}")
    if result.error:
        print(f"error  : {result.error}")
    print(
        "timings: "
        f"total={result.total_time_s:.3f}s "
        f"decision(policy+actor)={result.decision_time_s:.3f}s "
        f"observation={result.observation_time_s:.3f}s "
        f"execution={result.execution_time_s:.3f}s"
    )
    print(
        f"counts : observations={result.observation_count} "
        f"actions={result.action_count} steps={len(result.steps)}"
    )


def _save_screenshot(result: RunResult, path_arg: str) -> None:
    observation = result.context.last_observation
    if observation is None or not isinstance(observation.payload, bytes):
        print(f"warning: no image observation available for {path_arg}", file=sys.stderr)
        return
    path = Path(path_arg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(observation.payload)
    except OSError as exc:
        # Opt-in debug output must not change the task's own exit status.
        print(f"warning: failed to save screenshot to {path}: {exc}", file=sys.stderr)
        return
    print(f"screenshot: {path} ({len(observation.payload)} bytes)")


async def _run(args: argparse.Namespace) -> int:
    async with AsyncExitStack() as stack:
        actor, actor_label = _resolve_actor(args)
        if isinstance(actor, VLMActor):
            stack.push_async_callback(actor.aclose)
        io = await _launch_io(args)
        stack.push_async_callback(io.close)

        _print_header(args, io, actor_label)
        runner = Runner(
            policy=BasicPolicy(actor),
            observer=io,
            executor=io,
            task=args.task,
            max_actions=args.max_steps,
            max_observations=args.max_observations,
        )
        result = await runner.run()
        _print_result(result)
        if args.save_screenshot:
            _save_screenshot(result, args.save_screenshot)
        return 0 if result.success else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - keep the CLI outcome machine-readable
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
