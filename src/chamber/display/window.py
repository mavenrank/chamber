"""Playwright-backed, platform-neutral Chamber Desk window."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PWError

from chamber.browser.discovery import BrowserBuild
from chamber.display.adapter import DisplayAdapter

log = logging.getLogger(__name__)

_DIST = Path(__file__).with_name("dist")
_SCRIPT_RE = re.compile(r'<script([^>]*?)src=["\']([^"\']+)["\']([^>]*)></script>', re.I)
_STYLE_RE = re.compile(r'<link([^>]*?)href=["\']([^"\']+\.css)["\']([^>]*)>', re.I)


def _asset_path(src: str) -> Path:
    """Resolve one Vite asset inside the packaged display bundle."""
    root = _DIST.resolve()
    path = (root / src.lstrip("/\\")).resolve()
    if root not in path.parents:
        raise ValueError(f"display asset escaped bundle: {src}")
    return path


def _app_html() -> str:
    """Inline Vite's built assets so the app works from an about:blank page."""
    index = _DIST / "index.html"
    html = index.read_text(encoding="utf-8")

    def script(match: re.Match[str]) -> str:
        _before, src, _after = match.groups()
        body = _asset_path(src).read_text(encoding="utf-8")
        # Vite emits an IIFE for this bundle. Replacing the external module with a
        # plain inline script is more reliable in Playwright's about:blank document
        # than retaining a module/crossorigin attribute without a URL.
        return (
            "<script>document.addEventListener('DOMContentLoaded', function () {\n"
            f"{body}\n"
            "});</script>"
        )

    def stylesheet(match: re.Match[str]) -> str:
        _before, src, _after = match.groups()
        body = _asset_path(src).read_text(encoding="utf-8")
        return f"<style>{body}</style>"

    html = _SCRIPT_RE.sub(script, html)
    return _STYLE_RE.sub(stylesheet, html)


class ChamberDesk:
    """A small custom Chromium app window fed by a normalized event stream."""

    def __init__(
        self,
        pw: Playwright,
        build: BrowserBuild,
        *,
        width: int = 460,
        height: int = 720,
        headless: bool = False,
        on_action: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self.pw = pw
        self.build = build
        self.width = max(380, width)
        self.height = max(560, height)
        self.headless = headless
        self.on_action = on_action
        self.adapter = DisplayAdapter()
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._push_task: asyncio.Task[None] | None = None
        self._pending_snapshot: dict[str, object] | None = None
        self._closed = False
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def started(self) -> bool:
        return self.page is not None and not self.page.is_closed() and not self._closed

    async def start(self, *, task: str = "") -> bool:
        """Open the custom window. Failure is non-fatal to the browser loop."""
        if self.started:
            if task:
                self.handle("run_start", {"task": task})
            return True
        self._closing = False
        self._closed = False
        try:
            self.browser = await self.pw.chromium.launch(
                executable_path=str(self.build.path),
                headless=self.headless,
                args=[
                    "--app=about:blank",
                    f"--window-size={self.width},{self.height}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation"],
            )
            self.context = await self.browser.new_context(
                no_viewport=True,
                color_scheme="dark",
                locale="en-US",
            )
            self.page = await self.context.new_page()
            await self.page.expose_binding("__chamberDeskAction", self._action_binding)
            await self.page.expose_binding("__chamberQuery", self._query_binding)
            await self.page.set_content(_app_html(), wait_until="domcontentloaded")
            await self.page.title()
            self.page.on("close", self._on_page_close)
            self._closed = False
            self._closing = False
            if task:
                self.handle("run_start", {"task": task})
            else:
                self._schedule_push(self.adapter.snapshot())
            return True
        except Exception as exc:
            log.warning("Chamber Desk could not open: %s", exc)
            await self.close()
            return False

    def handle(self, event: str, payload: Mapping[str, Any] | None = None) -> None:
        """Apply an event synchronously and queue a best-effort UI update."""
        snapshot = self.adapter.handle(event, payload)
        self._schedule_push(snapshot)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._closed = True
        if self._push_task is not None and not self._push_task.done():
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
        self._push_task = None
        self._pending_snapshot = None
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.close()
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await self.browser.close()
        self.page = None
        self.context = None
        self.browser = None
        self._close_task = None

    # --------------------------------------------------------------- transport

    def _schedule_push(self, snapshot: dict[str, object]) -> None:
        if not self.started:
            return
        self._pending_snapshot = snapshot
        if self._push_task is None or self._push_task.done():
            self._push_task = asyncio.create_task(self._drain_pushes())

    async def _drain_pushes(self) -> None:
        while self._pending_snapshot is not None and self.started:
            snapshot = self._pending_snapshot
            self._pending_snapshot = None
            try:
                assert self.page is not None
                await self.page.evaluate(
                    "snapshot => window.__chamberDesk?.apply(snapshot)", snapshot
                )
            except (PWError, AssertionError):
                return
            except Exception as exc:
                log.debug("Chamber Desk update failed: %s", exc)
                return

    async def _action_binding(self, _source: object, action: object) -> None:
        if self.on_action is None:
            return
        try:
            result = self.on_action(str(action))
            if result is not None:
                await result
        except Exception:
            log.debug("Chamber Desk action failed", exc_info=True)

    async def _query_binding(self, _source: object, query: object) -> dict[str, object]:
        """Read-only Console queries for Phase 1. Never raises to the page.

        Desk × Console contract: this binding is transport #2 for
        `display/queries.py` (same functions as `/api/query`). It adds no
        server dependency to Desk — the window works identically without it.
        """
        try:
            from chamber.display import queries as _q

            payload = query if isinstance(query, dict) else {"op": str(query)}
            op = str(payload.get("op", ""))
            if op == "list_runs":
                try:
                    limit = int(payload.get("limit", 20) or 20)
                except (TypeError, ValueError):
                    limit = 20
                return {"ok": True, "runs": _q.list_runs(limit=min(max(limit, 1), 50))}
            if op == "get_run":
                return {"ok": True, **_q.get_run(str(payload.get("run_id", "")))}
            if op == "run_state":
                return {"ok": True, **_q.run_state(str(payload.get("run_id", "")))}
            if op == "thought_loop":
                try:
                    since = int(payload.get("since_step", 0) or 0)
                except (TypeError, ValueError):
                    since = 0
                return {
                    "ok": True,
                    **_q.thought_loop(str(payload.get("run_id", "")), since_step=since),
                }
            if op == "managed_runs":
                from chamber import supervisor as _sup

                return {"ok": True, "runs": _sup.managed_runs()}
            if op == "thread":
                return {"ok": True, **_q.thread(str(payload.get("run_id", "")))}
            if op == "usage":
                return {"ok": True, **_q.usage_by_model()}
            if op == "exchange_detail":
                return {
                    "ok": True,
                    **_q.exchange_detail(
                        str(payload.get("run_id", "")), str(payload.get("exchange_id", ""))
                    ),
                }
            if op == "list_profiles":
                return {"ok": True, "profiles": _q.list_profiles()}
            if op == "get_environment":
                return {"ok": True, **_q.get_environment()}
            if op == "get_snapshot":
                return {"ok": True, "snapshot": self.adapter.snapshot()}
            return {"ok": False, "error": f"unknown query op: {op}"}
        except Exception as exc:
            log.debug("Chamber Desk query failed", exc_info=True)
            return {"ok": False, "error": str(exc)[:300]}

    def _on_page_close(self, _page: Page) -> None:
        self._closed = True
        # Closing the small window should not leave a second Chromium process
        # running after the user dismisses it. ``close`` is idempotent and is
        # scheduled rather than awaited from Playwright's page event callback.
        if self.browser is not None and not self._closing:
            with contextlib.suppress(RuntimeError):
                self._close_task = asyncio.create_task(self.close())
