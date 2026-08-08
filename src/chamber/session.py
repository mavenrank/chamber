"""`Chamber` — the object everything else hangs off.

One browser window, a set of tabs, and the machinery to observe and act on
whichever one is in front. Both front-ends (the CLI loop and the MCP server) are
thin wrappers over this class, and an embedder can use it directly:

    async with Chamber.open() as ch:
        await ch.goto("https://example.com")
        print(ch.render())                       # what a model would see
        await ch.act({"action": "click", "ref": "e4"})

Tabs get short ids (`t1`, `t2`) rather than Playwright handles because those ids go
into prompts, and a model needs something it can say back. Every tab carries its
own overlay and its own console/network buffers, so switching tabs never loses the
history of the one you left.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PWError,
)

from chamber import paths
from chamber.actions.result import ActionResult, Outcome
from chamber.actions.schema import AnyAction, parse_action
from chamber.browser import launcher
from chamber.browser import ready as ready_mod
from chamber.browser.devtools import PageMonitor
from chamber.browser.discovery import BrowserBuild
from chamber.clipboard import Clipboard
from chamber.config import ChamberConfig
from chamber.dom import serialize
from chamber.dom.model import Snapshot
from chamber.dom.snapshot import ExtractOptions, capture
from chamber.interrupt import (
    Challenge,
    ChallengeKind,
    Nagging,
    detect_challenge,
    find_blocker,
    hand_off,
)
from chamber.overlay import Overlay
from chamber.overlay import install as install_overlay

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Tab:
    """One tab, with everything attached to it."""

    id: str
    page: Page
    overlay: Overlay
    monitor: PageMonitor
    opened_at: float = field(default_factory=time.time)
    # What this tab is being kept open for. Shown in every observation so a tab
    # doubles as a note to self — "amazon.in cart" is memory the model cannot lose
    # between steps, unlike anything it merely remembers.
    purpose: str = ""
    # Which tab spawned this one. Sites that open results in a new tab, and then a
    # third when that one links out, turn "where do I go back to" into a real
    # question several times a run. Recording the answer beats asking the model to
    # remember it across the twenty steps in between.
    opened_by: str = ""

    @property
    def closed(self) -> bool:
        return self.page.is_closed()


class _Popup:
    """Holds a tab that a click opened, if one appeared."""

    __slots__ = ("page",)

    def __init__(self) -> None:
        self.page: Page | None = None


class Chamber:
    """A live browser session."""

    def __init__(
        self,
        config: ChamberConfig,
        pw: Playwright,
        context: BrowserContext,
        build: BrowserBuild,
        run_id: str,
    ) -> None:
        self.config = config
        self.build = build
        self.run_id = run_id
        self._pw = pw
        self._context = context
        self._tabs: dict[str, Tab] = {}
        self._current: str = ""
        self._tab_seq = 0
        self.last_snapshot: Snapshot | None = None
        self.last_challenge: Challenge | None = None
        # Chamber's own clipboard — deliberately *not* the operating system's.
        # The agent copying a product title must not wipe whatever the user had on
        # their real clipboard, for the same reason the cursor is synthetic.
        # It also means copied text goes page → buffer → page without ever
        # entering the model's context: no tokens, and no transcription errors.
        self.clipboard = Clipboard()
        # How often each host has interrupted us. Dismissing the same sign-in modal
        # forty times is not progress, and this is what notices.
        self.nagging = Nagging()
        # Set by the agent loop so handoffs and traces can name the goal.
        self.goal: str = ""
        # Flipped by the Take Control button in the page. The loop waits on this
        # before every step, so pressing it genuinely stops the agent rather than
        # only changing how the bar looks.
        self.controlled = False
        self._control_listeners: list = []

        # Optional screenshot-describing model. Attached here rather than owned by
        # the loop so `screenshot` describes the image for any caller — the CLI
        # agent, an MCP client, or a script using Chamber directly.
        from chamber.agent.vision import Vision

        self.vision: Vision | None = (
            Vision(config.vision, *config.vision_fallbacks) if config.vision else None
        )

        from chamber.actions.executor import Executor

        self._executor = Executor(self)
        context.on("page", self._on_new_page)

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, config: ChamberConfig | None = None):
        """Launch a browser and yield a session. Closes cleanly on exit."""
        config = config or ChamberConfig.from_env()
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        pw = await async_playwright().start()
        try:
            result = await launcher.launch(pw, config.browser)
            context = result.context

            # Init scripts must be installed before the first page navigates, or the
            # overlay and the readiness observer miss the load they most need to see.
            ch = cls(config, pw, context, result.build, run_id)
            await install_overlay(
                context, ch._on_control, skip_hosts=config.browser.overlay_skip_hosts
            )
            await ready_mod.install(context)

            await ch._adopt_existing()
            try:
                yield ch
            finally:
                await ch.close()
        finally:
            with contextlib.suppress(Exception):
                await pw.stop()

    async def _adopt_existing(self) -> None:
        """Take ownership of whatever the browser opened with.

        A persistent context normally opens with exactly one page. If a previous
        run left more behind — a crash before cleanup, or a Chromium build that
        restored a session despite the preference — collapse to one, so a run
        always starts from a predictable window.
        """
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            pages = [await self._context.new_page()]

        keeper, strays = pages[0], pages[1:]
        for page in strays:
            with contextlib.suppress(Exception):
                await page.close()
        if strays:
            log.info("closed %d leftover tab(s) from a previous session", len(strays))

        self._register(keeper)
        self._current = next(iter(self._tabs))

    async def close(self) -> None:
        for tab in list(self._tabs.values()):
            with contextlib.suppress(Exception):
                await tab.monitor.close()
        if self.vision is not None:
            # Hand the VRAM back before closing. Without this the model sits
            # resident for the full keep_alive window after the run is over —
            # 2.2 GB of an 8 GB card, doing nothing.
            with contextlib.suppress(Exception):
                await self.vision.release()
            with contextlib.suppress(Exception):
                await self.vision.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await self._context.close()

    # ---------------------------------------------------------------------- tabs

    def _register(self, page: Page, *, opened_by: str = "") -> Tab:
        self._tab_seq += 1
        tab_id = f"t{self._tab_seq}"
        tab = Tab(
            id=tab_id,
            page=page,
            overlay=Overlay(page, skip_hosts=self.config.browser.overlay_skip_hosts),
            monitor=PageMonitor(page),
            opened_by=opened_by,
        )
        self._tabs[tab_id] = tab
        page.on("close", lambda _p, tid=tab_id: self._tabs.pop(tid, None))
        return tab

    def _on_new_page(self, page: Page) -> None:
        """A tab the page opened itself — target=_blank, window.open, an ad."""
        if any(t.page is page for t in self._tabs.values()):
            return
        tab = self._register(page, opened_by=self._current)
        log.info("new tab %s (from %s): %s", tab.id, tab.opened_by or "?", page.url[:80])

    def home_tab(self, tab_id: str = "") -> str:
        """The tab this one was opened from, if it is still around.

        What "go back to where I was" means when a click opened a new tab instead of
        navigating. Falls back to the oldest surviving tab, which is reliably the
        one the run started from.
        """
        tab = self._tabs.get(tab_id or self._current)
        if tab is not None:
            parent = self._tabs.get(tab.opened_by)
            if parent is not None and not parent.closed:
                return parent.id
        live = [t for t in self._tabs.values() if not t.closed]
        return live[0].id if live else ""

    def _current_tab(self) -> Tab:
        """The tab everything acts on, healing if it has gone away.

        A tab can close underneath us at any moment — the page called
        `window.close()`, the user closed it, a popup finished. Every accessor has
        to survive that, not just `page`: an earlier version looked `overlay` up
        with a bare `self._tabs[self._current]` and a closed tab raised
        `KeyError: 't1'` from the executor's *cleanup* line, outside its try block,
        which ended a fifty-step run over a cosmetic HUD update.
        """
        tab = self._tabs.get(self._current)
        if tab is not None and not tab.closed:
            return tab

        live = [t for t in self._tabs.values() if not t.closed]
        if not live:
            raise RuntimeError("no open tabs")
        self._current = live[-1].id
        # The snapshot described a tab that is gone; its refs are meaningless now.
        self.last_snapshot = None
        log.info("current tab vanished; switched to %s", self._current)
        return live[-1]

    @property
    def page(self) -> Page:
        return self._current_tab().page

    @property
    def overlay(self) -> Overlay:
        return self._current_tab().overlay

    @property
    def monitor(self) -> PageMonitor:
        return self._current_tab().monitor

    @property
    def current_tab_id(self) -> str:
        return self._current

    def tab_ids(self) -> list[str]:
        return [t.id for t in self._tabs.values() if not t.closed]

    def tab_summary(self) -> list[dict[str, str]]:
        out = []
        for tab in self._tabs.values():
            if tab.closed:
                continue
            try:
                out.append(
                    {
                        "id": tab.id,
                        "url": tab.page.url,
                        "title": "",
                        "purpose": tab.purpose or _implied_purpose(tab.page.url),
                        "opened_by": tab.opened_by,
                    }
                )
            except PWError:
                continue
        return out

    async def open_tab(self, url: str = "about:blank", purpose: str = "") -> str:
        opener = self._current
        page = await self._context.new_page()
        tab = next((t for t in self._tabs.values() if t.page is page), None) or self._register(
            page, opened_by=opener
        )
        tab.purpose = purpose
        if not tab.opened_by:
            tab.opened_by = opener
        self._current = tab.id
        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await self.wait_ready()
        await page.bring_to_front()
        return tab.id

    async def adopt_tab(self, page: Page, *, focus: bool = True) -> str:
        tab = next((t for t in self._tabs.values() if t.page is page), None)
        if tab is None:
            tab = self._register(page)
        if focus:
            self._current = tab.id
            with contextlib.suppress(PWError):
                await page.bring_to_front()
            await self.wait_ready()
        return tab.id

    async def switch_tab(self, tab_id: str) -> bool:
        tab = self._tabs.get(tab_id)
        if tab is None or tab.closed:
            return False
        self._current = tab_id
        with contextlib.suppress(PWError):
            await tab.page.bring_to_front()
        self.last_snapshot = None
        return True

    async def close_tab(self, tab_id: str) -> bool:
        tab = self._tabs.get(tab_id)
        if tab is None:
            return False
        with contextlib.suppress(Exception):
            await tab.monitor.close()
        with contextlib.suppress(Exception):
            await tab.page.close()
        self._tabs.pop(tab_id, None)
        if self._current == tab_id:
            live = self.tab_ids()
            if not live:
                await self.open_tab()
            else:
                self._current = live[-1]
                with contextlib.suppress(PWError):
                    await self._tabs[self._current].page.bring_to_front()
        self.last_snapshot = None
        return True

    @contextlib.asynccontextmanager
    async def expect_possible_popup(self):
        """Catch a tab that a click opens, without failing when none appears.

        Playwright's `expect_page` raises on timeout, which makes it wrong for the
        common case: most clicks open nothing, and a click that opens nothing is
        not an error.
        """
        popup = _Popup()

        def on_page(page: Page) -> None:
            if popup.page is None:
                popup.page = page

        self._context.on("page", on_page)
        try:
            yield popup
            # A tab opened by a click arrives a beat after the click returns.
            if popup.page is None:
                await asyncio.sleep(0.25)
        finally:
            self._context.remove_listener("page", on_page)

    # --------------------------------------------------------------- navigation

    async def goto(self, url: str) -> ActionResult:
        return await self.act({"action": "navigate", "url": url})

    async def wait_ready(self, *, timeout_ms: int | None = None) -> ready_mod.Readiness:
        result = await ready_mod.wait_until_ready(
            self.page, timeout_ms=timeout_ms if timeout_ms is not None else 12_000
        )
        return result

    # -------------------------------------------------------------- observation

    async def observe(
        self,
        *,
        options: ExtractOptions | None = None,
        wait: bool = True,
        quiet: bool = False,
        check_challenge: bool = True,
    ) -> Snapshot:
        """Take a fresh look at the current tab.

        `quiet=True` skips the readiness wait and the challenge probe — used by the
        executor's internal re-resolve, where the page state is already known and
        the only thing needed is fresh refs.
        """
        notices: list[str] = []

        if wait and not quiet:
            readiness = await self.wait_ready()
            note = readiness.note
            if note:
                notices.append(note)

        if check_challenge and not quiet:
            challenge = await detect_challenge(self.page)
            blocker = await find_blocker(self.page)

            # A dismissible nag over a working page is not a login wall, even though
            # it contains a password field — and "sign in to keep reading" modals
            # serve exactly that, on every page of a site, all day. Left
            # undowngraded, `detect_challenge` hands the window to the human once
            # per page and the run becomes a queue of pointless sign-in prompts.
            # The page behind the modal is the evidence: if it is still there, we
            # are being nagged, not stopped.
            if challenge.kind is ChallengeKind.LOGIN and blocker.is_nag:
                log.info("login prompt is a dismissible overlay; treating it as a nag")
                challenge = Challenge(ChallengeKind.NONE)

            self.last_challenge = challenge if challenge.blocking else None
            if challenge.blocking:
                notices.append(
                    f"{challenge.detail} {challenge.prompt()} "
                    "Use ask_human rather than trying to work around it."
                )
            elif blocker.is_nag:
                notices.append(
                    f"Something is in the way: {blocker.describe()}. "
                    "Call dismiss_overlay to close it, then carry on."
                )
            elif blocker.is_wall:
                notices.append(
                    f"{blocker.describe()}. The page is not usable behind it — "
                    "if it wants an account, ask_human with reason 'login'."
                )

        snap = await capture(self.page, options, notices=notices)
        snap.tab_id = self._current
        snap.open_tabs = self.tab_summary()
        for tab in snap.open_tabs:
            if tab["id"] == self._current:
                tab["title"] = snap.title
        self.last_snapshot = snap
        return snap

    def render(self, **kwargs: Any) -> str:
        """The last observation, as the model would read it."""
        if self.last_snapshot is None:
            return "(no observation taken yet — call observe() first)"
        return serialize.render(self.last_snapshot, **kwargs)

    # ------------------------------------------------------------------ acting

    async def act(self, action: AnyAction | dict[str, Any]) -> ActionResult:
        """Validate and execute one action.

        Accepts a raw dict so callers — including the MCP server and hand-written
        scripts — never have to import the schema.
        """
        if isinstance(action, dict):
            try:
                action = parse_action(action)
            except Exception as exc:
                return ActionResult.failure(
                    Outcome.INVALID_ACTION,
                    str(action.get("action", "?")),
                    _explain_validation(exc),
                )
        return await self._executor.run(action)

    # ------------------------------------------------------------------ output

    async def screenshot(
        self, *, full_page: bool = False, name: str = "", scale: float = 1.0
    ) -> Path:
        """Capture the current tab. `scale < 1` shrinks it in the browser.

        Scaling matters more than it looks. A vision model's cost is driven by
        image *area* — Qwen2.5-VL tiles the input dynamically — so a full-size
        1440×900 screenshot took 107 seconds to describe on a laptop GPU, which is
        unusable inside a loop. Halving each dimension quarters the tiles.

        Done through CDP rather than Pillow: the resize happens in the compositor,
        before the PNG is ever encoded, so it is faster *and* costs no dependency.
        """
        out = paths.run_dir(self.run_id) / (name or f"shot-{int(time.time() * 1000)}.png")

        if scale >= 0.999:
            await self.page.screenshot(path=str(out), full_page=full_page)
            return out

        try:
            metrics = await self.monitor.send("Page.getLayoutMetrics")
            viewport = metrics["cssVisualViewport"]
            content = metrics["cssContentSize"]
            clip = {
                "x": 0.0,
                "y": 0.0,
                "width": float(content["width"] if full_page else viewport["clientWidth"]),
                "height": float(content["height"] if full_page else viewport["clientHeight"]),
                "scale": scale,
            }
            shot = await self.monitor.send(
                "Page.captureScreenshot",
                {"format": "png", "clip": clip, "captureBeyondViewport": full_page},
            )
            out.write_bytes(base64.b64decode(shot["data"]))
        except Exception as exc:
            log.debug("scaled capture failed (%s); falling back to full size", exc)
            await self.page.screenshot(path=str(out), full_page=full_page)
        return out

    async def look(self, question: str = "", *, full_page: bool = False) -> tuple[Path, str]:
        """Screenshot the page and have the vision model describe it.

        Returns the path either way. Without a vision model configured the
        description says so plainly rather than pretending — a planner told
        "screenshot saved to shot-123.png" and nothing else will keep asking for
        screenshots it cannot see, which is exactly the loop this avoids.
        """
        path = await self.screenshot(full_page=full_page, scale=self.config.loop.vision_scale)
        if self.vision is None:
            return path, (
                "(no vision model is configured, so nobody looked at this image. "
                "Set CHAMBER_VISION_MODEL to enable it. Rely on the control list "
                "and page content instead — do not take more screenshots.)"
            )
        return path, await self.vision.look(path, question)

    # ---------------------------------------------------------- handing over

    def _on_control(self, controlled: bool) -> None:
        """The Take Control button, from the page."""
        self.controlled = controlled
        log.info("human %s control", "took" if controlled else "gave back")
        for listener in self._control_listeners:
            try:
                listener(controlled)
            except Exception:
                log.debug("control listener raised", exc_info=True)

    def on_control_change(self, listener) -> None:
        self._control_listeners.append(listener)

    async def take_control(self, controlled: bool = True) -> None:
        """Hand the browser over (or take it back) from Python."""
        self.controlled = controlled
        await self.overlay.set_control(controlled)

    async def wait_while_controlled(self, poll_s: float = 0.4) -> float:
        """Block for as long as the human holds control. Returns seconds waited.

        Polling rather than an event because the button lives in the page and a
        navigation re-creates the overlay; the page is the authority on whether
        the human still has it, and asking is cheap.
        """
        if not self.controlled:
            return 0.0
        started = time.monotonic()
        await self.overlay.say("paused", "Agent stopped. Press Give control back when ready.", "plan")
        while self.controlled:
            await asyncio.sleep(poll_s)
            # The binding may have been lost (a page that blocked it, a crash);
            # re-reading the page keeps a stuck pause from being permanent.
            with contextlib.suppress(Exception):
                if not await self.overlay.is_controlled():
                    self.controlled = False
        return time.monotonic() - started

    # ------------------------------------------------------------ human in loop

    async def ask_human(
        self, question: str, *, reason: str = "blocked", resume_when: str = "", timeout_s: float = 300.0
    ) -> str:
        result = await hand_off(
            self.page,
            self.overlay,
            question=question,
            reason=reason,
            resume_when=resume_when,
            timeout_s=timeout_s,
        )
        if result.resolved:
            self.last_snapshot = None
        return result.for_model()

    # ---------------------------------------------------------------- reporting

    async def extension_status(self) -> list[dict[str, Any]]:
        """What extensions actually loaded — worth logging once at startup."""
        return await launcher.extension_report(self._context)


def _implied_purpose(url: str) -> str:
    """A label for a tab nobody named.

    Tabs the *page* opened never get a purpose, and on a busy run those are most of
    them. An unnamed row in the tab list is a tab the model cannot tell apart from
    three others, so the host stands in until something better is set — which an
    embedder can do at any time by assigning to `Tab.purpose`.
    """
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    if not host or url.startswith("about:"):
        return ""
    return host


def _explain_validation(exc: Exception) -> str:
    """Turn a pydantic error into something a model can fix on the next try.

    Pydantic's default rendering is a multi-line report with types, URLs and input
    echoes; a small model reading that often "fixes" the wrong thing. One line per
    problem, naming the field and what was wrong, works far better.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)[:400]
    lines = []
    for err in errors()[:6]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "function-after")
        msg = err.get("msg", "invalid")
        lines.append(f"{loc or 'action'}: {msg}")
    return "; ".join(lines) or str(exc)[:400]
