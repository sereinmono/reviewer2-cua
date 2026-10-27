"""Optional compatibility layers for the nano-cua base (kept outside the base).

Two independent pieces are provided:

* ``playwright_browser`` -- a Playwright/browser group implementing both the
  Observer and ActionExecutor ports (screenshot observation + click/type/key
  action space), with the matching model prompt and proposal parser.
* ``vlm`` -- a generic OpenAI-compatible VLM Actor that receives its system
  prompt and proposal parser from an action-space group.

``demo_cli`` wires both into the unmodified nano-cua Runner as a runnable demo.
"""

__version__ = "0.1.0"
