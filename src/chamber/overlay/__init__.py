"""The visible layer, from Python.

Wraps `overlay.js`. Every method is best-effort: an overlay call must never be the
reason a task fails. If the page navigated mid-call, or a CSP blocks the injection,
or the frame detached, the agent should carry on doing its job invisibly rather
than crash — so failures here are logged at debug level and swallowed.

The one thing this does *not* do is move the operating system's mouse pointer.
Playwright's mouse and CDP's Input domain synthesize events inside the browser; the
user's actual cursor stays where they left it. The visible pointer is a read-out.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PWError

log = logging.getLogger(__name__)

Status = Literal["idle", "busy", "ok", "warn", "err"]

_OVERLAY_JS = Path(__file__).with_name("overlay.js")

# Hosts where the overlay is not injected at all. Empty by default — the overlay is
# the point of this project, and staying off a page is the exception.
#
# The exception exists because a strict Content-Security-Policy can admit the bar's
# markup into the shadow root while blocking the stylesheet that makes it a bar.
# What renders then is a line of unstyled text across the top of the page with the
# layout pushed down to fit it: strictly worse than having no bar, and it is the
# *page* that ends up looking broken rather than the agent.
#
# Callers set this per deployment, because which sites do it is a property of the
# sites, not of chamber. `CHAMBER_OVERLAY_SKIP_HOSTS` sets it from the environment.
#
# It is a display decision only. Nothing about how the agent works changes: the
# cursor still moves as synthetic events, actions still execute, and the terminal
# still shows every step. What is lost is the on-page read-out.
DEFAULT_SKIP_HOSTS: tuple[str, ...] = ()


@cache
def source() -> str:
    return _OVERLAY_JS.read_text(encoding="utf-8")


def _skips(url: str, hosts: tuple[str, ...]) -> bool:
    """Is this URL on a host the overlay stays off?"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


async def install(
    context: BrowserContext,
    on_control: Callable[[bool], None] | None = None,
    on_prefs: Callable[[dict[str, object] | None], dict[str, object]] | None = None,
    *,
    skip_hosts: tuple[str, ...] = DEFAULT_SKIP_HOSTS,
    show_panel: bool = True,
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

    if on_prefs is not None:

        def _prefs_binding(
            _source: object, patch: dict[str, object] | None = None
        ) -> dict[str, object]:
            try:
                return on_prefs(patch if isinstance(patch, dict) else None)
            except Exception:
                log.debug("overlay preferences callback raised", exc_info=True)
                return {}

        with contextlib.suppress(PWError):
            await context.expose_binding("__chamberOverlayPrefs", _prefs_binding)

    # The skip list and panel visibility are handed to the script rather than
    # compiled into it. The old overlay remains fully installed in window mode so
    # its cursor, highlights, and control API keep working; only its panel is
    # hidden while Chamber Desk is the read-out.
    preamble = (
        f"window.__chamberSkipHosts = {json.dumps(list(skip_hosts))};"
        f" window.__chamberOverlayPanel = {json.dumps(show_panel)};"
    )
    await context.add_init_script(f"{preamble}\n{source()}")


class Overlay:
    """Per-page handle. Cheap to construct; holds no state of its own."""

    __slots__ = ("enabled", "page", "skip_hosts")

    def __init__(
        self,
        page: Page,
        *,
        enabled: bool = True,
        skip_hosts: tuple[str, ...] = DEFAULT_SKIP_HOSTS,
    ) -> None:
        self.page = page
        self.enabled = enabled
        self.skip_hosts = skip_hosts

    @property
    def suppressed(self) -> bool:
        """Is the overlay deliberately absent on the page currently loaded?

        Checked per call rather than fixed at construction because one tab
        navigates between hosts — a listing and then the site it links out to — and
        the answer changes underneath a long-lived `Overlay`.
        """
        if not self.enabled:
            return True
        try:
            return _skips(self.page.url, self.skip_hosts)
        except PWError:
            return False

    async def _call(self, expr: str, *args: object) -> object | None:
        if self.suppressed:
            return None
        try:
            return await self.page.evaluate(expr, *args)
        except PWError as exc:
            log.debug("overlay call failed (%s): %s", expr[:40], exc)
            return None

    async def ensure(self) -> bool:
        """Inject on demand.

        Needed for pages that were already open before `install()` ran, and as a
        recovery path when a page's CSP stripped the init script. The skip check
        matters here in particular: this path evaluates the source directly, so
        without it the on-demand route would re-inject exactly what the init script
        was told to leave alone.
        """
        if self.suppressed:
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
        """Show an attention block inside the corner panel; `head=None` clears it."""
        await self._call(
            "([h, s]) => window.__chamberOverlay?.banner(h, s)", [head, sub]
        )

    async def cursor_state(self) -> dict[str, float] | None:
        result = await self._call("() => window.__chamberOverlay?.state()")
        return result if isinstance(result, dict) else None

    # --- handing the browser over -------------------------------------------

    async def say(self, tag: str, text: str, kind: str = "") -> None:
        """Replace the live line and retain the previous item in panel history."""
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
