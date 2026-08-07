"""The visible layer, from Python.

Wraps `overlay.js`. Every method is best-effort: an overlay call must never be the
reason a task fails. If the page navigated mid-call, or a CSP blocks the injection,
or the frame detached, the agent should carry on doing its job invisibly rather
than crash — so failures here are logged at debug level and swallowed.

The one thing this does *not* do is move the operating system's mouse pointer.
Playwright's mouse and CDP's Input domain synthesize events inside the browser; the
user's actual cursor stays where they left it. The dot is a read-out, not a driver.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Literal

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PWError

log = logging.getLogger(__name__)

Status = Literal["idle", "busy", "ok", "warn", "err"]

_OVERLAY_JS = Path(__file__).with_name("overlay.js")


@cache
def source() -> str:
    return _OVERLAY_JS.read_text(encoding="utf-8")


async def install(
    context: BrowserContext, on_control: Callable[[bool], None] | None = None
) -> None:
    """Arrange for the overlay to exist on every page, including after navigation.

    `add_init_script` runs before page scripts on every document — which is what
    makes the bar and the cursor survive a link click without the Python side
    noticing.

    `on_control` is called when the human presses Take Control. Bound through
    `expose_binding` so it is installed on every page in the context, including
    ones opened later: a pause button that only works on the tab that happened to
    be open first is worse than none.
    """
    if on_control is not None:

        def _binding(_source: object, controlled: bool) -> None:
            try:
                on_control(bool(controlled))
            except Exception:
                log.debug("control callback raised", exc_info=True)

        with contextlib.suppress(PWError):  # already bound on a reused context
            await context.expose_binding("__chamberOnControl", _binding)

    await context.add_init_script(source())


class Overlay:
    """Per-page handle. Cheap to construct; holds no state of its own."""

    __slots__ = ("enabled", "page")

    def __init__(self, page: Page, *, enabled: bool = True) -> None:
        self.page = page
        self.enabled = enabled

    async def _call(self, expr: str, *args: object) -> object | None:
        if not self.enabled:
            return None
        try:
            return await self.page.evaluate(expr, *args)
        except PWError as exc:
            log.debug("overlay call failed (%s): %s", expr[:40], exc)
            return None

    async def ensure(self) -> bool:
        """Inject on demand.

        Needed for pages that were already open before `install()` ran, and as a
        recovery path when a page's CSP stripped the init script.
        """
        if not self.enabled:
            return False
        try:
            present = await self.page.evaluate("() => !!window.__chamberOverlay")
            if not present:
                await self.page.evaluate(source())
            return True
        except PWError as exc:
            log.debug("overlay injection failed: %s", exc)
            return False

    # --- cursor -------------------------------------------------------------

    async def move_to(self, x: float, y: float, ms: int = 340) -> None:
        await self._call(
            "([x, y, ms]) => window.__chamberOverlay?.moveTo(x, y, ms)", [x, y, ms]
        )

    async def click_at(self, x: float, y: float) -> None:
        await self._call("([x, y]) => window.__chamberOverlay?.click(x, y)", [x, y])

    # --- element focus ------------------------------------------------------

    async def highlight(self, box: tuple[int, int, int, int], label: str = "") -> None:
        await self._call(
            "([box, label]) => window.__chamberOverlay?.highlight(box, label)",
            [list(box), label],
        )

    async def clear_highlight(self) -> None:
        await self._call("() => window.__chamberOverlay?.clearHighlight()")

    # --- reasoning read-out -------------------------------------------------

    async def think(
        self,
        *,
        goal: str | None = None,
        thought: str | None = None,
        action: str | None = None,
        step: str | None = None,
        status: Status | None = None,
    ) -> None:
        """Update the HUD. Only the fields you pass change."""
        patch = {
            k: v
            for k, v in (
                ("goal", goal),
                ("thought", thought),
                ("action", action),
                ("step", step),
                ("status", status),
            )
            if v is not None
        }
        if patch:
            await self._call("(p) => window.__chamberOverlay?.think(p)", patch)

    async def set_visible(self, visible: bool) -> None:
        await self._call("(v) => window.__chamberOverlay?.hud(v)", visible)

    # --- handing control back ----------------------------------------------

    async def banner(self, head: str | None, sub: str = "") -> None:
        """Full-width takeover banner. `head=None` clears it."""
        await self._call(
            "([h, s]) => window.__chamberOverlay?.banner(h, s)", [head, sub]
        )

    async def cursor_state(self) -> dict[str, float] | None:
        result = await self._call("() => window.__chamberOverlay?.state()")
        return result if isinstance(result, dict) else None

    # --- handing the browser over -------------------------------------------

    async def say(self, tag: str, text: str, kind: str = "") -> None:
        """Append a line to the bar's rolling feed."""
        await self._call(
            "([t, x, k]) => window.__chamberOverlay?.say(t, x, k)", [tag, text, kind]
        )

    async def set_control(self, controlled: bool) -> None:
        """Set the Take Control state from Python — for `chamber setup`, and to
        reflect a resume the agent initiated rather than the button."""
        await self._call("(v) => window.__chamberOverlay?.toggleControl(v)", controlled)

    async def is_controlled(self) -> bool:
        """Has the human taken over on this page?

        Read straight from the page rather than trusted from a cached flag: the
        button lives in the DOM and a fresh navigation re-creates the overlay, so
        the page is the authority.
        """
        return bool(await self._call("() => window.__chamberOverlay?.isControlled()"))
