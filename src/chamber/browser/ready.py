"""Deciding when a page is worth looking at.

The user's framing was exactly right: some pages are readable the moment the HTML
lands, and some are an empty shell until JavaScript finishes. A fixed sleep is
wrong for both — too slow for the first, too fast for the second.

So this waits on *evidence* rather than a clock, and stops as soon as the evidence
arrives:

* `readyState === "complete"` — necessary, nowhere near sufficient on an SPA.
* **DOM quiet** — no meaningful mutation for `quiet_ms`. This is the strongest
  single signal, and `ready.js` filters spinner churn out of it so an animation
  cannot hold the page "busy" forever.
* **No in-flight fetch/XHR** — counted at the API level, so cache hits count too.
* **Content actually exists** — a quiet, complete page with 40 characters of text
  is a shell whose render has not run. Waiting longer is the right call, and this is
  the check that catches it.

Everything is bounded by `timeout_ms`, and a timeout is never an error: the page is
returned as-is with a note saying it never settled. The agent can usually work with
a half-loaded page, and refusing to look at one helps nobody.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PWError

log = logging.getLogger(__name__)

_READY_JS = Path(__file__).with_name("ready.js")


@cache
def source() -> str:
    return _READY_JS.read_text(encoding="utf-8")


async def install(context: BrowserContext) -> None:
    await context.add_init_script(source())


@dataclass(slots=True)
class Readiness:
    settled: bool
    reason: str
    waited_ms: int
    probe: dict[str, Any]

    @property
    def note(self) -> str | None:
        """A line for the observation, when the page did not settle cleanly."""
        if self.settled:
            return None
        if self.probe.get("hasVisibleSpinner"):
            return (
                f"The page was still loading after {self.waited_ms}ms — a spinner is "
                "visible. What you see may be incomplete; wait_for a specific element "
                "if something is missing."
            )
        if self.probe.get("inflight"):
            return (
                f"{self.probe['inflight']} network request(s) still in flight after "
                f"{self.waited_ms}ms. Content may still arrive."
            )
        return f"The page never went quiet within {self.waited_ms}ms; reading it as-is."


async def _probe(page: Page) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            "() => window.__chamberReady ? window.__chamberReady.probe() : null"
        )
    except PWError:
        # Mid-navigation: the execution context was destroyed. Not an error, just
        # "ask again in a moment".
        return {}
    if not result:
        # Init script missing (page predates install, or CSP). Fall back to what is
        # readable without instrumentation.
        try:
            return await page.evaluate(
                """() => ({
                    readyState: document.readyState,
                    quietMs: 9999, inflight: 0, mutations: 0, significant: 0, elapsedMs: 0,
                    textLength: (document.body?.innerText || '').trim().length,
                    nodeCount: document.getElementsByTagName('*').length,
                    hasVisibleSpinner: false,
                    uninstrumented: true,
                })"""
            )
        except PWError:
            return {}
    return result


async def wait_until_ready(
    page: Page,
    *,
    timeout_ms: int = 12_000,
    quiet_ms: int = 450,
    min_text: int = 120,
    poll_ms: int = 90,
) -> Readiness:
    """Block until the page looks worth reading, or the budget runs out."""
    start = time.monotonic()
    deadline = start + timeout_ms / 1000
    last: dict[str, Any] = {}
    saw_content = False

    while True:
        last = await _probe(page)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if last:
            complete = last.get("readyState") == "complete"
            quiet = last.get("quietMs", 0) >= quiet_ms
            idle = last.get("inflight", 0) == 0
            text_len = last.get("textLength", 0)
            has_content = text_len >= min_text or last.get("nodeCount", 0) > 150
            saw_content = saw_content or has_content

            if complete and quiet and idle and has_content:
                return Readiness(True, "settled", elapsed_ms, last)

            # A page that genuinely has almost no content — an error page, a bare
            # redirect stub — would otherwise burn the full timeout waiting for
            # text that is never coming. Once it has been complete, quiet and idle
            # for a good while, accept it.
            if complete and idle and last.get("quietMs", 0) >= quiet_ms * 4:
                return Readiness(True, "settled-sparse", elapsed_ms, last)

        if time.monotonic() >= deadline:
            reason = "timeout-empty" if not saw_content else "timeout-busy"
            log.debug("readiness timeout on %s: %s", page.url[:80], last)
            return Readiness(False, reason, elapsed_ms, last)

        await asyncio.sleep(poll_ms / 1000)


async def wait_for_text(page: Page, text: str, *, timeout_ms: int = 15_000) -> bool:
    """Wait for visible text. Case-insensitive, and checks rendered text rather than
    HTML so a match inside a `<script>` tag does not count."""
    needle = text.lower()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            found = await page.evaluate(
                "(t) => (document.body?.innerText || '').toLowerCase().includes(t)", needle
            )
            if found:
                return True
        except PWError:
            pass
        await asyncio.sleep(0.15)
    return False
