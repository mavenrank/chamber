"""Taking a snapshot, and re-finding an element later.

`capture()` runs the in-page script for controls and geometry, then hands the
serialized DOM to `reader.py` for readable content. One evaluate, one `content()`,
per step.

`resolve()` is the durability half. Coordinates captured at snapshot time are stale
the moment anything re-renders or scrolls, and clicking a stale coordinate is the
classic way an agent "succeeds" while hitting the wrong thing. So every action
re-resolves its ref immediately before executing: live handle if the page has not
navigated, replayed path if it has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from chamber.dom import reader
from chamber.dom.model import Element, Scrollable, Snapshot, Viewport

log = logging.getLogger(__name__)

_EXTRACT_JS = Path(__file__).with_name("extract.js")


@cache
def _extract_source() -> str:
    return _EXTRACT_JS.read_text(encoding="utf-8")


# Replays an element path (with shadow-root hops) and reports a fresh box. Kept
# separate from extract.js because it runs on the hot path — once per action —
# and must stay small.
_RESOLVE_JS = """
(payload) => {
  const { ref, path, wantPoint } = payload;
  let el = null;
  let via = "path";

  // Fast path: the node captured at snapshot time, if it is still in the document.
  // `isConnected` is the check that matters — a framework re-render leaves the old
  // node alive in memory but detached, and acting on a detached node silently
  // does nothing.
  const live = ref && window.__chamber && window.__chamber.elements.get(ref);
  if (live && live.isConnected) {
    el = live;
    via = "live";
  }

  if (!el) {
    let scope = document;
    for (const hop of path) {
      if (hop.kind === "shadow") {
        if (!el || !el.shadowRoot) return { ok: false, reason: "shadow-root-gone" };
        scope = el.shadowRoot;
        continue;
      }
      try {
        el = scope.querySelector(hop.sel);
      } catch (e) {
        return { ok: false, reason: "bad-selector: " + hop.sel };
      }
      if (!el) return { ok: false, reason: "no-match: " + hop.sel };
    }
  }
  if (!el) return { ok: false, reason: "empty-path" };

  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return { ok: false, reason: "zero-size", el: true };

  const style = getComputedStyle(el);
  const hidden =
    style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity) === 0;
  if (hidden) return { ok: false, reason: "hidden", el: true };

  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const inViewport = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;

  let occluded = false, by = null;
  if (wantPoint && inViewport) {
    let top = document.elementFromPoint(cx, cy);
    while (top && top.shadowRoot) {
      const inner = top.shadowRoot.elementFromPoint(cx, cy);
      if (!inner || inner === top) break;
      top = inner;
    }
    if (top && top.closest && top.closest("[data-chamber-overlay]")) {
      // our own HUD never counts
    } else if (top && top !== el && !el.contains(top) && !top.contains(el)) {
      occluded = true;
      by = top.tagName.toLowerCase() + (top.id ? "#" + top.id : "");
    }
  }

  return {
    ok: true,
    via,
    box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    point: [Math.round(cx), Math.round(cy)],
    inViewport,
    occluded,
    occludedBy: by,
    disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || "").trim().slice(0, 80),
  };
}
"""


@dataclass(slots=True)
class ExtractOptions:
    # 120, not 220. Measured on Wikipedia: 3,859 candidates, and at 220 the control
    # list took 8,300 characters against 6,200 of actual content — the model was
    # reading twice as much navigation as article. Past roughly the top hundred the
    # additions are footer links and edit-section anchors, which crowd out the thing
    # the task is about. Anything genuinely missing is one `scroll` away, and the
    # list says how many were omitted.
    max_elements: int = 120
    viewport_only: bool = False
    occlusion_check: bool = True
    text_budget: int = 6000
    keep_links: bool = True
    whole_page: bool = False
    read_content: bool = True


@dataclass(slots=True)
class Resolved:
    """A ref, re-checked against the live page right before it is used."""

    ok: bool
    reason: str = ""
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    point: tuple[int, int] = (0, 0)
    in_viewport: bool = False
    occluded: bool = False
    occluded_by: str | None = None
    disabled: bool = False
    tag: str = ""
    text: str = ""
    via: str = ""  # "live" = original node still connected, "path" = re-located


async def capture(
    page: Page,
    opts: ExtractOptions | None = None,
    *,
    notices: list[str] | None = None,
) -> Snapshot:
    """One full observation of the page."""
    opts = opts or ExtractOptions()

    # extract.js is a bare function expression; wrap it so Playwright's single
    # argument reaches it. `arguments` is unavailable in the arrow-function scope
    # Playwright evaluates in, so the explicit parameter is required.
    raw: dict[str, Any] = await page.evaluate(
        f"(opts) => ({_extract_source()})(opts)",
        {
            "maxElements": opts.max_elements,
            "viewportOnly": opts.viewport_only,
            "occlusionCheck": opts.occlusion_check,
            # reader.py does this better; only fall back to the in-page digest if
            # the HTML round trip fails.
            "textDigest": False,
            "stampRefs": True,
        },
    )

    content = ""
    read_stats: dict[str, Any] = {}
    if opts.read_content:
        try:
            html = await page.content()
            result = reader.read(
                html,
                base_url=raw.get("url", ""),
                budget=opts.text_budget,
                keep_links=opts.keep_links,
                whole_page=opts.whole_page,
            )
            content = result.text
            read_stats = {
                "main": result.main_selector,
                "html_chars": result.chars_in,
                "text_chars": result.chars_out,
                "link_density": result.link_density,
                "truncated": result.truncated,
            }
        except Exception as exc:
            # A detached frame or a navigation mid-capture kills content(); the
            # control list is still valid and is the half the agent acts on.
            log.debug("content read failed: %s", exc)
            read_stats = {"error": type(exc).__name__}

    stats = dict(raw.get("stats") or {})
    stats["read"] = read_stats

    snap = Snapshot(
        url=raw.get("url", page.url),
        title=raw.get("title", ""),
        viewport=Viewport.from_js(raw.get("viewport") or {}),
        elements=[Element.from_js(e) for e in raw.get("elements") or []],
        scrollables=[Scrollable.from_js(s) for s in raw.get("scrollables") or []],
        content=content,
        ready_state=raw.get("readyState", "complete"),
        stats=stats,
        notices=list(notices or []),
    )

    if not snap.elements:
        snap.notices.append(
            "No interactive elements found. The page may still be rendering, or the "
            "content may be inside a cross-origin iframe."
        )
    return snap


async def resolve(page: Page, element: Element, *, want_point: bool = True) -> Resolved:
    """Re-find an element on the live page. Called immediately before every action."""
    if not element.path and not element.ref:
        return Resolved(ok=False, reason="no-path")
    try:
        raw = await page.evaluate(
            _RESOLVE_JS,
            {"ref": element.ref, "path": element.path, "wantPoint": want_point},
        )
    except Exception as exc:
        return Resolved(ok=False, reason=f"evaluate-failed: {type(exc).__name__}")

    if not raw.get("ok"):
        return Resolved(ok=False, reason=str(raw.get("reason", "unknown")))

    box = raw["box"]
    point = raw["point"]
    return Resolved(
        ok=True,
        box=(box[0], box[1], box[2], box[3]),
        point=(point[0], point[1]),
        in_viewport=bool(raw.get("inViewport")),
        occluded=bool(raw.get("occluded")),
        occluded_by=raw.get("occludedBy"),
        disabled=bool(raw.get("disabled")),
        tag=raw.get("tag", ""),
        text=raw.get("text", ""),
        via=raw.get("via", ""),
    )
