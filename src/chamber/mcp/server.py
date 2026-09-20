"""Chamber over MCP — the same window, driven by someone else's agent.

Claude Code, opencode, or any MCP harness becomes the brain; chamber stays the
visible hands. Everything the built-in loop gets — the ref system, the repair pass,
the cursor, the captcha handoff — applies identically here, because this file is a
thin wrapper over `Chamber.act()` rather than a second implementation.

Two design choices worth stating:

**The browser opens lazily.** An MCP server is typically launched when the harness
starts, which may be long before anyone wants a browser. Opening a window at
startup would put an unwanted Brave on the user's screen every session, so the
first tool call is what launches it.

**Tools return the fresh page state, not just "ok".** The observation an agent
needs after a click is the one thing it always needs, and making it a separate
round trip doubles the calls for no benefit. Every action here answers with what
happened *and* what the page looks like now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mcp.server import MCPServer

from chamber.config import ChamberConfig
from chamber.dom import serialize
from chamber.session import Chamber

log = logging.getLogger(__name__)


class _Holder:
    """Owns the single browser session for the life of the server."""

    def __init__(self, config: ChamberConfig) -> None:
        self.config = config
        self._chamber: Chamber | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Chamber:
        async with self._lock:
            if self._chamber is None:
                self._stack = contextlib.AsyncExitStack()
                self._chamber = await self._stack.enter_async_context(
                    Chamber.open(self.config)
                )
                log.info("browser up: %s", self._chamber.build.label)
                await self._chamber.overlay.think(
                    goal="driven over MCP",
                    thought="Connected to an external agent.",
                    status="idle",
                )
                self._chamber._display_event(
                    "thought",
                    {"text": "Connected to an external agent.", "source": "MCP"},
                )
            return self._chamber

    async def close(self) -> None:
        async with self._lock:
            if self._stack is not None:
                await self._stack.aclose()
            self._stack = None
            self._chamber = None


def build_server(config: ChamberConfig) -> tuple[MCPServer, _Holder]:
    holder = _Holder(config)
    server = MCPServer(
        name="chamber",
        version="0.9.0",
        instructions=(
            "A real, visible browser you drive by naming element refs.\n\n"
            "Call `observe` first — it returns the page as a list of controls "
            "(`[e12] button \"Add to cart\"`) plus the readable content. Act by "
            "citing a ref. Refs are reassigned on every observation, so always use "
            "the ones from the most recent result and never a remembered one.\n\n"
            "Every action returns the fresh page state, so you rarely need to call "
            "`observe` twice in a row.\n\n"
            "A human is watching this window. Never solve a captcha and never type "
            "credentials — call `ask_human` instead."
        ),
    )

    # ---------------------------------------------------------------- helpers

    async def _state(ch: Chamber, *, content: bool = True) -> str:
        await ch.observe()
        return ch.render(include_content=content)

    async def _act(payload: dict[str, Any], *, content: bool = True) -> str:
        ch = await holder.get()
        result = await ch.act(payload)
        body = result.for_model()
        if result.data.get("content"):
            return f"{body}\n\n{result.data['content']}"
        if result.data.get("lines"):
            return body + "\n" + "\n".join(str(line) for line in result.data["lines"])
        return f"{body}\n\n{await _state(ch, content=content)}"

    # ------------------------------------------------------------- observation

    @server.tool(
        description=(
            "Look at the current page. Returns interactive controls with refs, the "
            "readable content, scroll position and open tabs. Start here."
        )
    )
    async def observe(include_content: bool = True, max_controls: int = 120) -> str:
        ch = await holder.get()
        await ch.observe()
        return ch.render(include_content=include_content, max_controls=max_controls)

    @server.tool(
        description=(
            "Read the full page text with a larger budget. Use whole_page=true on "
            "search results and dashboards, where the main-content heuristic would "
            "discard the list you want."
        )
    )
    async def read_page(budget: int = 16000, whole_page: bool = False) -> str:
        return await _act({"action": "read_page", "budget": budget, "whole_page": whole_page})

    # -------------------------------------------------------------- navigation

    @server.tool(description="Go to a URL in the current tab.")
    async def navigate(url: str) -> str:
        return await _act({"action": "navigate", "url": url})

    @server.tool(description="Go back in this tab's history.")
    async def go_back() -> str:
        return await _act({"action": "go_back"})

    @server.tool(description="Reload the current page.")
    async def reload() -> str:
        return await _act({"action": "reload"})

    # ------------------------------------------------------------- interaction

    @server.tool(
        description=(
            "Click an element by ref, e.g. 'e12'. The cursor visibly moves there "
            "first. Fails with a clear reason if the element is covered, disabled "
            "or gone."
        )
    )
    async def click(ref: str, why: str = "", button: str = "left", clicks: int = 1) -> str:
        return await _act(
            {"action": "click", "ref": ref, "why": why, "button": button, "clicks": clicks}
        )

    @server.tool(description="Type into a text field by ref. Set submit=true to press Enter after.")
    async def type_text(ref: str, text: str, submit: bool = False, clear: bool = True, why: str = "") -> str:
        return await _act(
            {"action": "type_text", "ref": ref, "text": text, "submit": submit, "clear": clear, "why": why}
        )

    @server.tool(description="Press a key, e.g. 'Enter', 'Escape', 'Tab', 'Control+a'.")
    async def press_key(key: str, ref: str | None = None) -> str:
        return await _act({"action": "press_key", "key": key, "ref": ref})

    @server.tool(description="Choose an option in a <select> by its visible text.")
    async def select_option(ref: str, value: str) -> str:
        return await _act({"action": "select_option", "ref": ref, "value": value})

    @server.tool(
        description=(
            "Attach files to a file input by ref. A file picker is an OS dialog and "
            "cannot be clicked; this sets the input directly. Only use paths the "
            "user gave you."
        )
    )
    async def upload_file(ref: str, paths: list[str]) -> str:
        return await _act({"action": "upload_file", "ref": ref, "paths": paths})

    @server.tool(
        description=(
            "Close a modal, banner or sign-in prompt covering the page, and say "
            "whether the page is usable again. Never clicks Sign in or Register to "
            "make one go away."
        )
    )
    async def dismiss_overlay() -> str:
        return await _act({"action": "dismiss_overlay"})

    @server.tool(description="Hover an element — opens menus and tooltips.")
    async def hover(ref: str) -> str:
        return await _act({"action": "hover", "ref": ref})

    @server.tool(
        description=(
            "Scroll the window, or a named panel by ref. direction: up|down|top|bottom. "
            "amount in pixels, 0 for one screenful."
        )
    )
    async def scroll(direction: str = "down", amount: int = 0, ref: str | None = None) -> str:
        return await _act({"action": "scroll", "direction": direction, "amount": amount, "ref": ref})

    @server.tool(description="Wait for text to appear, a selector to become visible, or a fixed delay.")
    async def wait_for(text: str | None = None, selector: str | None = None, ms: int = 0) -> str:
        return await _act({"action": "wait_for", "text": text, "selector": selector, "ms": ms})

    # -------------------------------------------------------------------- tabs

    @server.tool(description="List open tabs with their ids and URLs.")
    async def list_tabs() -> str:
        ch = await holder.get()
        tabs = ch.tab_summary()
        lines = [
            f"{'▸' if t['id'] == ch.current_tab_id else ' '} [{t['id']}] {t['url']}"
            for t in tabs
        ]
        return f"{len(tabs)} open tab(s):\n" + "\n".join(lines)

    @server.tool(description="Open a new tab, optionally at a URL, and switch to it.")
    async def open_tab(url: str = "") -> str:
        return await _act({"action": "open_tab", "url": url})

    @server.tool(description="Switch to a tab by id, e.g. 't2'.")
    async def switch_tab(tab_id: str) -> str:
        return await _act({"action": "switch_tab", "tab_id": tab_id})

    @server.tool(description="Close a tab by id, or the current one if omitted.")
    async def close_tab(tab_id: str | None = None) -> str:
        return await _act({"action": "close_tab", "tab_id": tab_id})

    # ---------------------------------------------------------------- devtools

    @server.tool(
        description=(
            "Computed styles, box model, attributes, outer HTML and real event "
            "listeners for one element. This is inspect-element, as data."
        )
    )
    async def inspect(ref: str) -> str:
        ch = await holder.get()
        result = await ch.act({"action": "inspect", "ref": ref})
        if not result.ok:
            return result.for_model()
        import json

        return json.dumps(result.data, indent=2, default=str)[:8000]

    @server.tool(description="Console messages since this page loaded, including uncaught errors.")
    async def console_log(limit: int = 50, errors_only: bool = False) -> str:
        return await _act({"action": "console_log", "limit": limit, "errors_only": errors_only})

    @server.tool(description="The network log: method, URL, status, timing. Filter by substring or failures.")
    async def network_log(limit: int = 50, url_contains: str | None = None, failed_only: bool = False) -> str:
        return await _act(
            {
                "action": "network_log",
                "limit": limit,
                "url_contains": url_contains,
                "failed_only": failed_only,
            }
        )

    @server.tool(
        description=(
            "Run JavaScript in the page and return the result. Escape hatch — prefer "
            "the other tools, which are visible to the human watching and appear in "
            "the trace."
        )
    )
    async def evaluate_js(expression: str) -> str:
        ch = await holder.get()
        result = await ch.act({"action": "evaluate_js", "expression": expression})
        if not result.ok:
            return result.for_model()
        import json

        return json.dumps(result.data.get("value"), indent=2, default=str)[:8000]

    @server.tool(description="Take a screenshot. Returns the saved path.")
    async def screenshot(full_page: bool = False) -> str:
        ch = await holder.get()
        path = await ch.screenshot(full_page=full_page)
        return f"saved to {path}"

    # ------------------------------------------------------------ human in loop

    @server.tool(
        description=(
            "Hand the window to the person watching and wait. The correct response "
            "to a captcha, a login wall or a payment step — never try to work around "
            "one. reason: captcha|login|payment|ambiguous|blocked|confirm"
        )
    )
    async def ask_human(question: str, reason: str = "blocked", resume_when: str = "") -> str:
        ch = await holder.get()
        answer = await ch.ask_human(question, reason=reason, resume_when=resume_when)
        return f"{answer}\n\n{await _state(ch)}"

    # -------------------------------------------------------------- narration

    @server.tool(
        description=(
            "Show your current reasoning in the browser's on-screen HUD. Call this "
            "as you go — it is what makes your work legible to the human watching, "
            "and it costs one cheap round trip."
        )
    )
    async def narrate(thought: str, goal: str = "", status: str = "busy") -> str:
        ch = await holder.get()
        await ch.overlay.think(
            thought=thought,
            goal=goal or None,
            status=status if status in ("idle", "busy", "ok", "warn", "err") else "busy",
        )
        ch._display_event(
            "thought",
            {
                "text": thought,
                "goal": goal,
                "status": status,
                "source": "MCP",
            },
        )
        return "shown in Chamber Desk (and the retained browser HUD when enabled)"

    @server.tool(description="Browser, profile, extensions and current page — a one-shot status check.")
    async def status() -> str:
        ch = await holder.get()
        exts = await ch.extension_status()
        ext_line = (
            ", ".join(
                f"{e.get('name')} {e.get('version') or ''}"
                f"{'' if e.get('enabled') else ' (DISABLED)'}"
                for e in exts
            )
            or "none"
        )
        snap = ch.last_snapshot
        page = serialize.render_compact(snap) if snap else "(nothing observed yet)"
        return (
            f"browser:    {ch.build.label} (MV2 capable: {ch.build.mv2})\n"
            f"profile:    {ch.config.profile}\n"
            f"extensions: {ext_line}\n"
            f"tabs:       {len(ch.tab_ids())} open, current {ch.current_tab_id}\n"
            f"page:       {page}"
        )

    return server, holder


async def serve(config: ChamberConfig | None = None) -> None:
    """Run the MCP server on stdio until the client disconnects."""
    config = config or ChamberConfig.from_env()
    server, holder = build_server(config)
    try:
        await server.run_stdio_async()
    finally:
        await holder.close()
