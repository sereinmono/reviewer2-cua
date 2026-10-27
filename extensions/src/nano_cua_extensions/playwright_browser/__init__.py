"""Playwright/browser compatibility group for nano-cua.

The group is a single object implementing both nano-cua ports (screenshot
observer + browser action executor) over one Chromium session, plus the action
vocabulary, model system prompt and proposal parser that belong to this action
space. No nano-cua base code knows about any of this.

Typical composition::

    from nano_cua import BasicPolicy, Runner
    from nano_cua_extensions.playwright_browser import PlaywrightIO

    group = await PlaywrightIO.launch(headless=True, url="https://example.com")
    runner = Runner(policy=BasicPolicy(actor), observer=group, executor=group, task="...")
"""

from .browser_space import (
    ACTION_KINDS,
    BROWSER_SYSTEM_PROMPT,
    MAX_FINISH_REASON_CHARS,
    SPACE,
    action_from_payload,
    browser_proposal_parser,
    normalize_action,
    observation_viewport,
    proposal_from_payload,
    validate_click,
    validate_key,
    validate_text,
)
from .playwright_io import SCREENSHOT_KIND, SOURCE, PlaywrightIO

PlaywrightBrowserGroup = PlaywrightIO
"""Alias emphasizing the port-pair role; the same object serves both ports."""

__all__ = [
    "ACTION_KINDS",
    "BROWSER_SYSTEM_PROMPT",
    "MAX_FINISH_REASON_CHARS",
    "PlaywrightBrowserGroup",
    "PlaywrightIO",
    "SCREENSHOT_KIND",
    "SOURCE",
    "SPACE",
    "action_from_payload",
    "browser_proposal_parser",
    "normalize_action",
    "observation_viewport",
    "proposal_from_payload",
    "validate_click",
    "validate_key",
    "validate_text",
]
