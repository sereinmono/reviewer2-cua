"""End-to-end smoke test: real Chromium + local HTML + MockActor.

This is the only test that launches a browser. It skips cleanly when no
Playwright browser is installed, so the offline unit suite still runs anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

import nano_cua_extensions  # noqa: E402
from nano_cua import Action, BasicPolicy, Finish, MockActor, ObservationRequest, Runner  # noqa: E402
from nano_cua_extensions.playwright_browser import PlaywrightIO  # noqa: E402

DEMO_PAGE = Path(nano_cua_extensions.__file__).parent / "playwright_browser" / "demo_page.html"
VIEWPORT = (640, 480)
_MISSING_BROWSER_HINTS = ("Executable doesn't exist", "is not found", "playwright install")


async def _launch_or_skip() -> PlaywrightIO:
    errors: list[str] = []
    for channel in (None, "msedge", "chrome"):
        try:
            return await PlaywrightIO.launch(
                headless=True,
                viewport=VIEWPORT,
                url=DEMO_PAGE.resolve().as_uri(),
                browser_channel=channel,
            )
        except Exception as exc:  # noqa: BLE001 - probe which browser binaries exist
            if not any(hint in str(exc) for hint in _MISSING_BROWSER_HINTS):
                # A real launch regression must fail, not be reported as a skip.
                raise
            errors.append(f"{channel or 'bundled'}: {exc}")
    pytest.skip("no usable chromium browser: " + " | ".join(errors))


async def test_mock_actor_runs_observe_click_observe_finish_on_a_local_page():
    io = await _launch_or_skip()
    try:
        x, y = VIEWPORT[0] // 2, VIEWPORT[1] // 2
        actor = MockActor(
            [
                Action(space="browser", kind="click", args={"x": x, "y": y}),
                Finish(reason="mock: clicked the centre button", success=True),
            ]
        )
        runner = Runner(
            policy=BasicPolicy(actor),
            observer=io,
            executor=io,
            task="click the centre button",
            max_actions=2,
            max_observations=4,
        )
        result = await runner.run()

        assert result.success is True
        assert [step.command for step in result.steps] == ["observe", "execute", "observe", "finish"]
        assert result.observation_count == 2
        assert result.action_count == 1
        assert await io.page.inner_text("#status") == f"CLICKED 1 at ({x}, {y})"

        final = result.context.last_observation
        assert final is not None
        assert final.source == "playwright" and final.kind == "screenshot"
        assert isinstance(final.payload, bytes)
        assert final.payload.startswith(b"\x89PNG\r\n\x1a\n")
        assert final.metadata["width"] == VIEWPORT[0]
        assert final.metadata["height"] == VIEWPORT[1]
        assert result.observation_time_s > 0
        assert result.execution_time_s > 0
        assert result.total_time_s >= result.observation_time_s + result.execution_time_s
    finally:
        await io.close()


async def test_playwright_execute_never_captures_screenshots(monkeypatch):
    """Acceptance: only an explicit Observe command may hit page.screenshot."""
    io = await _launch_or_skip()
    screenshot_calls = 0
    original_screenshot = io.page.screenshot

    async def counting_screenshot(*args: object, **kwargs: object) -> bytes:
        nonlocal screenshot_calls
        screenshot_calls += 1
        return await original_screenshot(*args, **kwargs)

    monkeypatch.setattr(io.page, "screenshot", counting_screenshot)
    try:
        executed = await io.execute(Action(space="browser", kind="click", args={"x": 5, "y": 5}))
        assert executed.success is True
        assert screenshot_calls == 0

        observed = await io.observe(ObservationRequest())
        assert observed.kind == "screenshot"
        assert screenshot_calls == 1

        await io.execute(Action(space="browser", kind="click", args={"x": 6, "y": 6}))
        assert screenshot_calls == 1
    finally:
        await io.close()


async def test_executor_rejects_untrusted_actions_without_side_effects():
    io = await _launch_or_skip()
    try:
        out_of_bounds = await io.execute(Action(space="browser", kind="click", args={"x": 10_000, "y": 10_000}))
        assert out_of_bounds.success is False
        assert out_of_bounds.error is not None and "outside" in out_of_bounds.error

        wrong_space = await io.execute(Action(space="desktop", kind="click", args={"x": 1, "y": 1}))
        assert wrong_space.success is False
        assert wrong_space.error is not None and "space" in wrong_space.error

        generated_code = await io.execute(Action(space="browser", kind="run_js", args={"code": "alert(1)"}))
        assert generated_code.success is False
        assert generated_code.error is not None and "kind" in generated_code.error

        non_action = await io.execute("not-an-action")  # type: ignore[arg-type]
        assert non_action.success is False
        assert non_action.error is not None and "non-Action" in non_action.error

        status = await io.page.inner_text("#status")
        assert "Waiting for a click" in status
    finally:
        await io.close()


async def test_observer_rejects_unsupported_observation_requests():
    io = await _launch_or_skip()
    try:
        with pytest.raises(ValueError):
            await io.observe(ObservationRequest(kind="dom"))
    finally:
        await io.close()
