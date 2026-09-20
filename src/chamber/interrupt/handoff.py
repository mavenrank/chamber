"""Handing the window to the human, and taking it back.

The mechanism is deliberately dumb, because anything clever here fails in the one
case it matters. Three things happen, in order:

1. **Surface the tab.** `bring_to_front()` plus a window-level focus. If the person
   is looking at another application entirely, an unfocused tab quietly waiting is
   the same as no prompt at all.
2. **Say what is needed, in the page.** The corner activity panel expands with a
   focused attention block. Console output is easy to miss; the browser window is
   what the person is already looking at.
3. **Watch for the condition to clear.** Polling, not a keypress. Requiring the
   human to come back and press Enter in a terminal doubles the interruption; if
   they solved the captcha, the agent should just notice and carry on.

There is always a timeout, and it is always survivable — a run that hangs forever
waiting on a person who walked away is a worse failure than one that reports "I
needed help and nobody came".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from playwright.async_api import Error as PWError
from playwright.async_api import Page

from chamber.interrupt.detect import Challenge, ChallengeKind, detect_challenge
from chamber.overlay import Overlay

log = logging.getLogger(__name__)


@dataclass(slots=True)
class HandoffResult:
    resolved: bool
    waited_s: float
    reason: str

    def for_model(self) -> str:
        if self.resolved:
            return (
                f"The human dealt with it after {self.waited_s:.0f}s. "
                "The page has changed — look at the fresh observation before acting."
            )
        return (
            f"Nobody responded within {self.waited_s:.0f}s ({self.reason}). "
            "The page is unchanged. Consider a different route to the information, "
            "or finish with what you have and say what was blocked."
        )


async def hand_off(
    page: Page,
    overlay: Overlay,
    *,
    question: str,
    reason: str = "blocked",
    resume_when: str = "",
    timeout_s: float = 300.0,
    poll_s: float = 1.0,
    on_wait: callable[[float], None] | None = None,
) -> HandoffResult:
    """Block until the human clears the obstacle, or the timeout expires."""
    start = time.monotonic()

    try:
        await page.bring_to_front()
    except PWError as exc:
        log.debug("bring_to_front failed: %s", exc)

    heading = {
        "captcha": "Captcha — over to you",
        "login": "Sign-in needed",
        "payment": "Payment step — your call",
        "ambiguous": "Need a decision",
        "confirm": "Confirm before I continue",
    }.get(reason, "Your turn")

    sub = question
    if resume_when:
        sub += f"  ·  I'll resume when {resume_when}."
    await overlay.banner(heading, sub)
    await overlay.think(thought=question, status="warn", action="waiting for a human")

    baseline = await _page_signature(page)
    deadline = start + timeout_s
    resolved = False
    reason_out = "timed out"

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        elapsed = time.monotonic() - start
        if on_wait:
            on_wait(elapsed)

        if page.is_closed():
            reason_out = "the tab was closed"
            break

        # For a detectable challenge, the honest test is "is it still there".
        if reason in ("captcha", "login", "payment"):
            current = await detect_challenge(page)
            if not current.blocking:
                resolved = True
                reason_out = "the challenge cleared"
                break
            # A different challenge kind means progress, not resolution — keep
            # waiting but update what the banner is asking for.
            if current.kind is not _kind_for(reason):
                await overlay.banner(heading, f"{current.prompt()}")

        # Otherwise: any real navigation or content change counts as an answer.
        signature = await _page_signature(page)
        if signature != baseline and signature is not None:
            resolved = True
            reason_out = "the page changed"
            break

    waited = time.monotonic() - start
    await overlay.banner(None)
    await overlay.think(
        thought="Carrying on." if resolved else "Nobody answered; continuing without it.",
        status="busy" if resolved else "warn",
    )
    return HandoffResult(resolved=resolved, waited_s=waited, reason=reason_out)


def _kind_for(reason: str) -> ChallengeKind:
    return {
        "captcha": ChallengeKind.CAPTCHA,
        "login": ChallengeKind.LOGIN,
        "payment": ChallengeKind.PAYMENT,
    }.get(reason, ChallengeKind.NONE)


async def _page_signature(page: Page) -> str | None:
    """Coarse "has anything meaningful changed" key.

    URL plus a bucketed text length. Bucketed so an animated counter or a ticking
    clock does not read as the human having acted.
    """
    try:
        return await page.evaluate(
            """() => location.href + "#" +
               Math.round(((document.body?.innerText || "").trim().length) / 200)"""
        )
    except PWError:
        return None


async def announce(overlay: Overlay, challenge: Challenge) -> None:
    """Show a challenge in the HUD without blocking — used when the loop wants to
    tell the model about it and let the model decide to call `ask_human`."""
    await overlay.think(
        thought=f"{challenge.detail} {challenge.prompt()}",
        status="warn",
    )
