"""Telling a modal you can close from a wall you cannot.

Sites interrupt. A newsletter box, a consent wall, an app-download banner, "sign in
to keep reading" — the page is fine underneath, but something is sitting on top of
it and an agent that does not deal with the cover just fails repeatedly against it.

The trap is that the most common of these interruptions **contains a password
field**, and `detect.py` treats a password field as a `LOGIN` challenge. That is
right in general and wrong here, in an expensive way: the agent hands the window to
the human on every page that nags, and a long run becomes a queue of pointless
sign-in prompts rather than work.

The distinction that matters is not *what the overlay says* but **whether the page
works without it**:

* A **nag** sits on top of a page that is otherwise fine. There is real content
  behind it and something that closes it. Close it and carry on — no human needed.
  Measured on a real example: 99% of the viewport covered, with 6,444 characters of
  the actual page still rendered behind it.
* A **wall** is the page. Dismiss it and there is nothing underneath, or there is
  nothing to dismiss it with. That one is a genuine handoff.

So the test is content behind the overlay plus the presence of a closer, and the
text is only a tie-breaker. That survives restyling and translation, which a phrase
list does not.

One thing this module deliberately will *not* do is dismiss its way past a real
sign-in requirement. If the same host keeps re-nagging, `Nagging.escalated` flips
and the caller is told to ask the human to sign in once, by hand. Clicking "close"
forty times is not persistence, it is a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.async_api import Error as PWError
from playwright.async_api import Page

log = logging.getLogger(__name__)

# How much of the viewport something must cover before it counts as "in the way".
# Below this it is a cookie strip or a toast, which does not block reading.
_MIN_AREA_SHARE = 0.22

# Text remaining outside the overlay, above which the page is judged to still be
# there behind it. A login page has almost nothing outside its form.
_MIN_CONTENT_BEHIND = 600

_PROBE_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  const viewportArea = Math.max(vw * vh, 1);

  const CLOSER_RE = /close|dismiss|cross|skip|later|not now|no thanks|maybe later|×|✕|✖/i;
  // Anything that would sign us in, subscribe us, or agree to something is not a
  // "closer" even when it sits in the same corner. Clicking one of these to get rid
  // of a modal is how an agent accidentally creates an account.
  const TRAP_RE = /sign ?in|log ?in|register|join|continue with|agree|accept|allow|subscribe|apply/i;

  const describe = (el) => ({
    tag: el.tagName,
    aria: el.getAttribute('aria-label') || '',
    text: (el.innerText || '').trim().slice(0, 40),
    cls: (el.className || '').toString().slice(0, 80),
  });

  let best = null;
  for (const el of document.querySelectorAll('div,section,aside,dialog,[role=dialog],[role=alertdialog]')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    if (!['fixed', 'absolute', 'sticky'].includes(cs.position)) continue;

    const z = parseInt(cs.zIndex || '0', 10) || 0;
    const r = el.getBoundingClientRect();
    const share = (r.width * r.height) / viewportArea;
    if (share < 0.15) continue;

    // It has to actually be on top where it matters: sample the centre.
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), vh - 1);
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !(el === hit || el.contains(hit))) continue;

    const isDialog = el.getAttribute('role') === 'dialog'
      || el.getAttribute('role') === 'alertdialog'
      || el.tagName === 'DIALOG'
      || /modal|overlay|popup|dialog|lightbox/i.test((el.className || '').toString());
    if (z < 100 && !isDialog) continue;

    const score = share + (isDialog ? 1 : 0) + Math.min(z, 10000) / 100000;
    if (!best || score > best.score) best = { el, z, share, isDialog, score };
  }

  if (!best) return { present: false };

  const el = best.el;

  // Find something that closes it. Ordered: explicit aria first, then an icon
  // button, then text. Traps are filtered out entirely.
  const candidates = [...el.querySelectorAll('button,[role=button],a,svg,i,span')];
  let closer = null;
  for (const c of candidates) {
    const hay = [c.getAttribute('aria-label') || '', (c.className || '').toString(),
                 (c.innerText || '').slice(0, 30), c.getAttribute('title') || ''].join(' ');
    if (!CLOSER_RE.test(hay)) continue;
    if (TRAP_RE.test((c.innerText || '') + ' ' + (c.getAttribute('aria-label') || ''))) continue;
    const cr = c.getBoundingClientRect();
    if (cr.width < 6 || cr.height < 6) continue;
    closer = c;
    break;
  }

  // How much readable text is NOT inside the overlay? This is the load-bearing
  // signal: a nag leaves the page intact behind it, a wall does not.
  let behind = 0;
  for (const node of document.querySelectorAll('h1,h2,h3,p,li,td,article,section')) {
    if (el.contains(node)) continue;
    const cs2 = getComputedStyle(node);
    if (cs2.display === 'none' || cs2.visibility === 'hidden') continue;
    behind += (node.innerText || '').trim().length;
    if (behind > 5000) break;
  }

  if (closer) closer.setAttribute('data-chamber-closer', '1');
  el.setAttribute('data-chamber-overlay', '1');

  return {
    present: true,
    z: best.z,
    share: best.share,
    isDialog: best.isDialog,
    behind,
    closer: closer ? describe(closer) : null,
    label: (el.getAttribute('aria-label') || el.id || (el.className || '').toString()).slice(0, 90),
    text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 240),
    hasPassword: !!el.querySelector('input[type=password]'),
  };
}
"""


@dataclass(slots=True)
class Blocker:
    """Something covering the page, and what can be done about it."""

    present: bool = False
    dismissible: bool = False
    content_behind: int = 0
    area_share: float = 0.0
    label: str = ""
    text: str = ""
    closer: str = ""
    has_password: bool = False

    @property
    def is_nag(self) -> bool:
        """Closeable, with a working page behind it. Handle it and move on."""
        return (
            self.present
            and self.dismissible
            and self.content_behind >= _MIN_CONTENT_BEHIND
        )

    @property
    def is_wall(self) -> bool:
        """Nothing behind it, or no way past it. This one needs the human."""
        return self.present and not self.is_nag

    def describe(self) -> str:
        if not self.present:
            return "nothing is covering the page"
        what = self.label or "an overlay"
        cover = f"{self.area_share * 100:.0f}% of the window"
        if self.is_nag:
            return f"a dismissible overlay ({what}) covering {cover}; close: {self.closer}"
        return f"a blocking overlay ({what}) covering {cover} with no usable close control"


async def find_blocker(page: Page) -> Blocker:
    """What, if anything, is in the way right now."""
    try:
        probe = await page.evaluate(_PROBE_JS)
    except PWError:
        return Blocker()

    if not probe or not probe.get("present"):
        return Blocker()

    share = float(probe.get("share") or 0.0)
    if share < _MIN_AREA_SHARE:
        return Blocker()

    closer = probe.get("closer") or None
    desc = ""
    if closer:
        desc = (closer.get("aria") or closer.get("text") or closer.get("cls") or closer.get("tag") or "").strip()

    return Blocker(
        present=True,
        dismissible=bool(closer),
        content_behind=int(probe.get("behind") or 0),
        area_share=share,
        label=str(probe.get("label") or "")[:90],
        text=str(probe.get("text") or "")[:240],
        closer=desc[:60],
        has_password=bool(probe.get("hasPassword")),
    )


async def dismiss(page: Page, blocker: Blocker | None = None) -> tuple[bool, str]:
    """Try to close whatever is covering the page. Returns (gone, what happened).

    The close control is clicked as a real click rather than `el.click()` through
    JS, so React sees the same event sequence a person would produce. `find_blocker`
    stamps the element with `data-chamber-closer`, which is what makes it
    addressable from here without passing handles around.
    """
    blocker = blocker or await find_blocker(page)
    if not blocker.present:
        return True, "nothing was covering the page"

    tried: list[str] = []

    if blocker.dismissible:
        try:
            target = page.locator("[data-chamber-closer]").first
            await target.click(timeout=3000)
            tried.append(f"clicked {blocker.closer!r}")
        except PWError as exc:
            log.debug("closer click failed: %s", exc)
            tried.append("the close control would not take a click")

    after = await find_blocker(page)
    if not after.present:
        return True, "; ".join(tried) or "it went away"

    # Escape is the standard dismissal for a focus-trapped dialog and costs nothing.
    try:
        await page.keyboard.press("Escape")
        tried.append("pressed Escape")
    except PWError:
        pass

    after = await find_blocker(page)
    if not after.present:
        return True, "; ".join(tried)

    return False, "; ".join(tried) or "found no way to close it"


@dataclass
class Nagging:
    """Per-host memory of how often we have closed the same thing.

    Without this, an agent working through forty pages on one site will dismiss the
    same sign-in modal forty times, which is not progress — it is the same obstacle
    forty times. After `limit` the caller should stop dismissing and ask the human
    to sign in once, by hand, which clears all forty at once.
    """

    limit: int = 3
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, host: str) -> int:
        host = (host or "").lower()
        self.counts[host] = self.counts.get(host, 0) + 1
        return self.counts[host]

    def escalated(self, host: str) -> bool:
        return self.counts.get((host or "").lower(), 0) >= self.limit

    def advice(self, host: str) -> str:
        return (
            f"This is the {self.counts.get(host.lower(), 0)}th time {host} has put a "
            "sign-in prompt in the way. Closing it again will not help — the account "
            "wall is the real obstacle. Call ask_human with reason 'login' and ask "
            "them to sign in once in this window; the profile keeps the session for "
            "every later page."
        )


__all__ = ["Blocker", "Nagging", "dismiss", "find_blocker"]
