"""DevTools, as capabilities rather than a panel.

The user-facing ask is "let the agent open DevTools and diagnose things". What
DevTools actually *is* is a UI over the Chrome DevTools Protocol — so rather than
driving a panel the agent cannot read, chamber gives it the same protocol
underneath: console messages, the network log, computed styles, the box model,
event listeners, coverage, and raw CDP as an escape hatch.

(The visible panel is still available — `BrowserConfig(devtools_panel=True)` passes
`--auto-open-devtools-for-tabs` — but that is for the human watching, not for the
agent. An agent reading a screenshot of the Elements tree would be strictly worse
at this than one calling `DOM.getBoxModel`.)

Console and network are *buffered from page creation*, not queried on demand. An
error that fired during load is exactly the error worth seeing, and by the time the
agent thinks to ask, it is long gone.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import CDPSession, ConsoleMessage, Page, Request, Response

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ConsoleEntry:
    level: str
    text: str
    url: str = ""
    line: int = 0
    ts: float = field(default_factory=time.time)

    def render(self) -> str:
        where = f" ({self.url.rsplit('/', 1)[-1]}:{self.line})" if self.url else ""
        return f"[{self.level}]{where} {self.text}"


@dataclass(slots=True)
class NetworkEntry:
    method: str
    url: str
    resource_type: str = ""
    status: int | None = None
    failure: str | None = None
    ms: float = 0.0
    size: int = 0
    started: float = field(default_factory=time.time)

    @property
    def failed(self) -> bool:
        return self.failure is not None or (self.status is not None and self.status >= 400)

    def render(self) -> str:
        code = self.failure or (str(self.status) if self.status else "pending")
        return f"{code:>12}  {self.method:<6} {self.url[:110]}  {self.ms:.0f}ms"


class PageMonitor:
    """Rolling buffers of everything the page said, per page.

    Bounded deques rather than lists: a chatty SPA can emit thousands of console
    lines in a minute, and an unbounded buffer in a long-lived daemon is a leak
    with extra steps.
    """

    # No __slots__ here on purpose: Playwright caches its handler wrappers by
    # setattr-ing onto the object that owns the bound method, so a slotted class
    # cannot register page event listeners.

    def __init__(self, page: Page, *, console_max: int = 500, network_max: int = 800) -> None:
        self.page = page
        self.console: deque[ConsoleEntry] = deque(maxlen=console_max)
        self.network: deque[NetworkEntry] = deque(maxlen=network_max)
        self._by_request: dict[Request, NetworkEntry] = {}
        self._page_errors: deque[str] = deque(maxlen=100)
        self._cdp: CDPSession | None = None
        self._attach()

    def _attach(self) -> None:
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

    # --- listeners ----------------------------------------------------------

    def _on_console(self, msg: ConsoleMessage) -> None:
        loc = msg.location or {}
        self.console.append(
            ConsoleEntry(
                level=msg.type,
                text=msg.text[:1000],
                url=loc.get("url", ""),
                line=loc.get("lineNumber", 0),
            )
        )

    def _on_page_error(self, error: Exception) -> None:
        text = str(error)[:1500]
        self._page_errors.append(text)
        self.console.append(ConsoleEntry(level="uncaught", text=text))

    def _on_request(self, req: Request) -> None:
        entry = NetworkEntry(method=req.method, url=req.url, resource_type=req.resource_type)
        self._by_request[req] = entry
        self.network.append(entry)

    def _on_response(self, res: Response) -> None:
        entry = self._by_request.get(res.request)
        if entry is None:
            return
        entry.status = res.status
        entry.ms = (time.time() - entry.started) * 1000

    def _on_request_failed(self, req: Request) -> None:
        entry = self._by_request.get(req)
        if entry is None:
            return
        entry.failure = (req.failure or "failed")[:120]
        entry.ms = (time.time() - entry.started) * 1000

    # --- reads --------------------------------------------------------------

    def console_log(self, *, limit: int = 50, errors_only: bool = False) -> list[ConsoleEntry]:
        items = list(self.console)
        if errors_only:
            items = [e for e in items if e.level in ("error", "uncaught", "warning")]
        return items[-limit:]

    def network_log(
        self,
        *,
        limit: int = 50,
        url_contains: str | None = None,
        failed_only: bool = False,
    ) -> list[NetworkEntry]:
        items = list(self.network)
        if url_contains:
            needle = url_contains.lower()
            items = [e for e in items if needle in e.url.lower()]
        if failed_only:
            items = [e for e in items if e.failed]
        return items[-limit:]

    def clear(self) -> None:
        self.console.clear()
        self.network.clear()
        self._by_request.clear()

    # --- raw CDP ------------------------------------------------------------

    async def cdp(self) -> CDPSession:
        """A CDP session on this page, created once and reused."""
        if self._cdp is None:
            self._cdp = await self.page.context.new_cdp_session(self.page)
        return self._cdp

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        session = await self.cdp()
        return await session.send(method, params or {})

    async def close(self) -> None:
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                await self._cdp.detach()
            self._cdp = None


# ---------------------------------------------------------------- inspection

_INSPECT_JS = """
(payload) => {
  const { ref, path } = payload;
  let el = null;
  const live = ref && window.__chamber && window.__chamber.elements.get(ref);
  if (live && live.isConnected) el = live;
  if (!el) {
    let scope = document;
    for (const hop of path || []) {
      if (hop.kind === "shadow") { if (!el?.shadowRoot) return null; scope = el.shadowRoot; continue; }
      el = scope.querySelector(hop.sel);
      if (!el) return null;
    }
  }
  if (!el) return null;

  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();

  // The properties that actually explain layout and visibility problems. A full
  // computed-style dump is ~340 properties of mostly defaults — noise the model
  // pays for on every inspect.
  const INTERESTING = [
    "display","position","visibility","opacity","z-index","overflow","pointer-events",
    "width","height","margin","padding","border","box-sizing",
    "flex-direction","justify-content","align-items","gap","grid-template-columns",
    "color","background-color","font-size","font-weight","line-height","text-align",
    "transform","transition","cursor",
  ];
  const styles = {};
  for (const p of INTERESTING) {
    const v = cs.getPropertyValue(p);
    if (v && v !== "none" && v !== "normal" && v !== "auto" && v !== "0px") styles[p] = v;
  }

  const attrs = {};
  for (const a of el.attributes) attrs[a.name] = a.value.slice(0, 200);

  // getEventListeners() is a DevTools console API, not page script — approximate
  // by looking for inline handlers and the framework attributes that imply one.
  const handlerAttrs = [...el.attributes].map(a => a.name).filter(n => /^on|^v-on|^@|^ng-|^data-action/.test(n));

  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    classes: [...el.classList].slice(0, 20),
    attributes: attrs,
    box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    styles,
    handlerAttributes: handlerAttrs,
    text: (el.innerText || "").trim().slice(0, 400),
    html: el.outerHTML.slice(0, 1200),
    parent: el.parentElement
      ? el.parentElement.tagName.toLowerCase() +
        (el.parentElement.id ? "#" + el.parentElement.id : "") +
        (el.parentElement.className && typeof el.parentElement.className === "string"
          ? "." + el.parentElement.className.trim().split(/\\s+/).slice(0,2).join(".") : "")
      : null,
    childCount: el.children.length,
  };
}
"""


async def inspect_element(page: Page, ref: str, path: list[dict[str, str]]) -> dict[str, Any] | None:
    """Inspect-element, returned as data instead of a panel."""
    try:
        return await page.evaluate(_INSPECT_JS, {"ref": ref, "path": path})
    except Exception as exc:
        log.debug("inspect failed: %s", exc)
        return None


async def listeners_via_cdp(monitor: PageMonitor, ref: str) -> list[dict[str, Any]]:
    """Real event listeners, via `DOMDebugger.getEventListeners`.

    This is the one piece of "what is wired to this button" that page script
    genuinely cannot see — it needs the debugger's view of the JS heap, which is
    precisely why DevTools shows it and `document` does not.
    """
    try:
        doc = await monitor.send("DOM.getDocument", {"depth": 0})
        root_id = doc["root"]["nodeId"]
        node = await monitor.send(
            "DOM.querySelector", {"nodeId": root_id, "selector": f'[data-chamber-ref="{ref}"]'}
        )
        node_id = node.get("nodeId")
        if not node_id:
            return []
        obj = await monitor.send("DOM.resolveNode", {"nodeId": node_id})
        object_id = obj["object"]["objectId"]
        result = await monitor.send("DOMDebugger.getEventListeners", {"objectId": object_id})
        return [
            {
                "type": lis.get("type"),
                "useCapture": lis.get("useCapture"),
                "passive": lis.get("passive"),
                "once": lis.get("once"),
                "source": f"{lis.get('scriptId', '')}:{lis.get('lineNumber', 0)}",
            }
            for lis in result.get("listeners", [])
        ]
    except Exception as exc:
        log.debug("getEventListeners failed: %s", exc)
        return []
