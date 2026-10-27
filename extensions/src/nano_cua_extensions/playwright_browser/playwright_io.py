"""Playwright implementation of the Observer and ActionExecutor ports.

This is the Playwright/browser compatibility group: one object implements both
ports over a shared browser session. The nano-cua base only ever sees two
independent protocols, so this group (or any other environment group) can be
injected without changing base code. Nothing here is imported by the base.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from nano_cua import (
    Action,
    ActionResult,
    ActionValidationError,
    Observation,
    ObservationRequest,
    frozen_mapping,
)
from .browser_space import CLICK, PRESS_KEY, TYPE_TEXT, normalize_action

SOURCE = "playwright"
SCREENSHOT_KIND = "screenshot"


class PlaywrightIO:
    """Chromium page I/O behind the Observer / ActionExecutor boundaries."""

    def __init__(self, *, playwright: Playwright, browser: Browser, context: BrowserContext, page: Page) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._closed = False

    @property
    def page(self) -> Page:
        """Escape hatch for tests/debugging; not part of the port contract."""
        return self._page

    @classmethod
    async def launch(
        cls,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 720),
        url: str | None = None,
        browser_channel: str | None = None,
        browser_args: Sequence[str] | None = None,
        timeout_ms: int = 30_000,
    ) -> PlaywrightIO:
        """Start a browser + context + page and optionally open ``url``."""
        width, height = viewport
        if width < 1 or height < 1:
            raise ValueError(f"viewport must be positive, got {viewport!r}")
        playwright = await async_playwright().start()
        try:
            launch_kwargs: dict[str, Any] = {"headless": headless}
            if browser_channel is not None:
                launch_kwargs["channel"] = browser_channel
            if browser_args:
                launch_kwargs["args"] = list(browser_args)
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)
            io = cls(playwright=playwright, browser=browser, context=context, page=page)
            if url is not None:
                await io.goto(url)
            return io
        except BaseException:
            await playwright.stop()
            raise

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="load")

    async def observe(self, request: ObservationRequest) -> Observation:
        """Capture one PNG of the current page, in memory."""
        if self._closed:
            raise RuntimeError("PlaywrightIO is closed")
        if request.source not in (None, SOURCE):
            raise ValueError(f"unsupported observation source {request.source!r}; this observer produces {SOURCE!r}")
        if request.kind not in (None, SCREENSHOT_KIND):
            raise ValueError(f"unsupported observation kind {request.kind!r}; this observer produces {SCREENSHOT_KIND!r}")
        payload = await self._page.screenshot(type="png")
        title = await self._page.title()
        viewport = self._page.viewport_size or {"width": 0, "height": 0}
        metadata = frozen_mapping(
            {
                "url": self._page.url,
                "title": title,
                "content_type": "image/png",
                "width": int(viewport["width"]),
                "height": int(viewport["height"]),
            }
        )
        return Observation(
            source=SOURCE,
            kind=SCREENSHOT_KIND,
            payload=payload,
            captured_at=time.time(),
            metadata=metadata,
        )

    async def execute(self, action: Action) -> ActionResult:
        """Validate and apply one browser action; never runs generated code."""
        started = time.perf_counter()
        if self._closed:
            return ActionResult(success=False, error="PlaywrightIO is closed", duration_s=time.perf_counter() - started)
        if not isinstance(action, Action):
            return ActionResult(
                success=False,
                error=f"rejected non-Action value of type {type(action).__name__}",
                duration_s=time.perf_counter() - started,
            )
        viewport = self._page.viewport_size or {"width": 0, "height": 0}
        try:
            normalized = normalize_action(
                action,
                width=int(viewport["width"]) or None,
                height=int(viewport["height"]) or None,
            )
        except ActionValidationError as exc:
            return ActionResult(
                success=False,
                error=f"rejected {action.space}.{action.kind}: {exc}",
                duration_s=time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 - a malformed action must not escape
            return ActionResult(
                success=False,
                error=f"rejected {action.space}.{action.kind}: {type(exc).__name__}: {exc}",
                duration_s=time.perf_counter() - started,
            )
        try:
            await self._dispatch(normalized)
        except PlaywrightError as exc:
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 - report any executor failure to the policy
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.perf_counter() - started,
            )
        return ActionResult(
            success=True,
            duration_s=time.perf_counter() - started,
            output=frozen_mapping({"kind": normalized.kind}),
        )

    async def _dispatch(self, action: Action) -> None:
        if action.kind == CLICK:
            await self._page.mouse.click(float(action.args["x"]), float(action.args["y"]))
        elif action.kind == TYPE_TEXT:
            await self._page.keyboard.type(action.args["text"])
        elif action.kind == PRESS_KEY:
            await self._page.keyboard.press(action.args["key"])
        else:  # pragma: no cover - normalize_action already rejects unknown kinds
            raise ActionValidationError(f"unsupported action kind {action.kind!r}")

    async def close(self) -> None:
        """Best-effort ordered teardown; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        for closer in (self._context.close, self._browser.close, self._playwright.stop):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - teardown must not mask the task result
                pass

    async def __aenter__(self) -> PlaywrightIO:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
