"""Executing an action against the live page.

This is the durability layer the whole design leans on. Between "the model said
click e12" and "the browser clicked something" there are a dozen ways to be wrong,
and the executor's job is to close each one *before* the click rather than report a
mystery afterwards:

* **Re-resolve, always.** The ref is looked up on the live page immediately before
  use. A snapshot is a photograph; between the photograph and the click, a
  framework can have replaced every node on screen.
* **Repair what is mechanical.** Off screen → scroll it into view. Covered by
  something that scrolling fixes → scroll and re-check. These cost the model
  nothing and are reported back so it still learns how the page behaves.
* **Refuse what is not.** Disabled, genuinely covered by a modal, gone entirely —
  these come back as typed outcomes with a hint naming a different action to try.
* **Never raise into the loop.** Every handler returns an `ActionResult`. A
  Playwright exception is caught, classified, and turned into feedback, because an
  exception escaping here would end a run over something the model could have
  worked around in one step.

Actions are also *narrated*: the cursor glides to the target, the element gets a
labelled halo, the HUD says what is being attempted. That is not decoration — it is
the difference between watching an agent work and watching a window twitch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlparse

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from chamber.actions import schema as S
from chamber.actions.result import ActionResult, Outcome
from chamber.dom import serialize
from chamber.dom.model import Element
from chamber.dom.snapshot import Resolved, resolve
from chamber.interrupt.overlay_block import dismiss, find_blocker

if TYPE_CHECKING:
    from chamber.session import Chamber

log = logging.getLogger(__name__)

# Typing faster than this stops looking like typing and starts tripping the
# rate-limit heuristics some sites apply to input events.
_TYPE_DELAY_MS = 14
_MAX_TYPE_TIME_MS = 2500


class Target(NamedTuple):
    """A ref, resolved and made safe to act on."""

    el: Element
    res: Resolved
    repairs: list[str]


class Executor:
    """Runs one validated action. Holds no state between calls."""

    __slots__ = ("ch",)

    def __init__(self, chamber: Chamber) -> None:
        self.ch = chamber

    # ------------------------------------------------------------------ entry

    async def run(self, action: S.AnyAction) -> ActionResult:
        started = time.monotonic()
        name = action.action
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            return ActionResult.failure(
                Outcome.INVALID_ACTION, name, f"No handler for action {name!r}."
            )

        if self.ch.controlled:
            # A belt-and-braces stop. The loop already waits, but an action can be
            # issued from the MCP server or a script with no loop in front of it,
            # and "the human has the browser" must hold for every caller.
            return ActionResult.failure(
                Outcome.BROWSER_ERROR,
                name,
                "The human has taken control of the browser. Nothing will run until "
                "they hand it back with the button in the top bar.",
            )

        await self._narrate_safely(action=self._describe(action), status="busy")
        try:
            result = await handler(action)
        except PWTimeout as exc:
            result = ActionResult.failure(
                Outcome.TIMEOUT, name, str(exc).split("\n")[0][:200], retryable=True
            )
        except PWError as exc:
            result = self._classify_pw_error(name, exc)
        except Exception as exc:
            log.exception("unhandled error in %s", name)
            result = ActionResult.failure(
                Outcome.BROWSER_ERROR, name, f"{type(exc).__name__}: {exc}"[:200]
            )

        result.duration_ms = int((time.monotonic() - started) * 1000)
        # Outside the try above, so it needs its own guard: updating a status dot
        # must never be the thing that ends a run.
        await self._narrate_safely(status="ok" if result.ok else "err")
        return result

    async def _narrate_safely(self, **patch: Any) -> None:
        """HUD updates are decoration; they must not raise into the loop."""
        try:
            await self.ch.overlay.think(**patch)
        except Exception as exc:
            log.debug("overlay update skipped: %s", exc)

    @staticmethod
    def _classify_pw_error(name: str, exc: PWError) -> ActionResult:
        """Turn Playwright's message into an outcome the model can act on."""
        msg = str(exc).split("\n")[0][:200]
        low = msg.lower()
        if "not visible" in low or "not stable" in low or "intercepts pointer" in low:
            return ActionResult.failure(Outcome.NOT_INTERACTABLE, name, msg, retryable=True)
        if "detached" in low or "not attached" in low or "no node found" in low:
            return ActionResult.failure(Outcome.STALE_REF, name, msg, retryable=True)
        if "net::" in low or "navigation" in low:
            return ActionResult.failure(Outcome.NAVIGATION_FAILED, name, msg)
        if "target closed" in low or "closed" in low:
            return ActionResult.failure(Outcome.BROWSER_ERROR, name, "The tab was closed.")
        return ActionResult.failure(Outcome.BROWSER_ERROR, name, msg)

    @staticmethod
    def _describe(action: S.AnyAction) -> str:
        """The one-liner shown in the HUD."""
        data = action.model_dump(exclude_defaults=True, exclude={"why", "action"})
        bits = ", ".join(f"{k}={v!r}" for k, v in list(data.items())[:3])
        return f"{action.action}({bits})" if bits else action.action

    # ---------------------------------------------------------------- targeting

    async def _target(
        self, ref: str, action_name: str, *, need_point: bool = True
    ) -> Target | ActionResult:
        """Resolve a ref to something safe to act on, repairing what is repairable.

        Returns a `Target` on success or an `ActionResult` describing exactly why
        not — callers branch on the type, which keeps the failure path impossible
        to forget.
        """
        snap = self.ch.last_snapshot
        if snap is None:
            return ActionResult.failure(
                Outcome.UNKNOWN_REF, action_name, "No page observation has been taken yet."
            )

        el = snap.get(ref)
        if el is None:
            near = ", ".join(e.ref for e in snap.elements[:8])
            return ActionResult.failure(
                Outcome.UNKNOWN_REF,
                action_name,
                f"Ref {ref!r} is not in the current control list. Valid refs start: {near}",
            )

        repairs: list[str] = []
        res = await resolve(self.ch.page, el, want_point=need_point)

        if not res.ok:
            # One re-observation, then one more attempt. A single re-render between
            # observation and action is routine on a live app and should cost the
            # model nothing.
            await self.ch.observe(quiet=True, display_event=False)
            snap = self.ch.last_snapshot
            el2 = snap.get(ref) if snap else None
            if el2 is not None:
                res = await resolve(self.ch.page, el2, want_point=need_point)
                if res.ok:
                    el = el2
                    repairs.append("re-resolved after the page re-rendered")
        if not res.ok:
            return ActionResult.failure(
                Outcome.STALE_REF,
                action_name,
                f"{serialize.describe_element(el)} is no longer on the page ({res.reason}).",
                retryable=True,
            )

        if res.disabled:
            return ActionResult.failure(
                Outcome.DISABLED,
                action_name,
                f"{serialize.describe_element(el)} is disabled.",
            )

        if need_point and not res.in_viewport:
            await self._scroll_into_view(el)
            res = await resolve(self.ch.page, el, want_point=True)
            if not res.ok:
                return ActionResult.failure(
                    Outcome.STALE_REF, action_name, f"Lost the element while scrolling to it ({res.reason})."
                )
            repairs.append("scrolled it into view")

        if need_point and res.occluded:
            # Centring often clears a sticky header or a bottom bar, which is the
            # most common cause by far.
            await self._scroll_into_view(el, block="center")
            res2 = await resolve(self.ch.page, el, want_point=True)
            if res2.ok and not res2.occluded:
                res = res2
                repairs.append("re-centred it to clear an overlapping element")
            elif res2.ok:
                res = res2

        # Overwrite the snapshot's geometry with what we just measured; everything
        # downstream reads the fresh values.
        el.box = res.box
        el.point = res.point
        el.occluded = res.occluded
        el.occluded_by = res.occluded_by
        return Target(el, res, repairs)

    async def _scroll_into_view(self, el: Element, block: str = "nearest") -> None:
        try:
            await self.ch.page.evaluate(
                """([ref, path, block]) => {
                  let node = window.__chamber?.elements.get(ref);
                  if (!node || !node.isConnected) {
                    let scope = document; node = null;
                    for (const hop of path || []) {
                      if (hop.kind === "shadow") { if (!node?.shadowRoot) return; scope = node.shadowRoot; continue; }
                      node = scope.querySelector(hop.sel); if (!node) return;
                    }
                  }
                  node?.scrollIntoView({ block, inline: "nearest", behavior: "instant" });
                }""",
                [el.ref, el.path, block],
            )
            # One frame for the scroll to land before we re-measure.
            await self.ch.page.wait_for_timeout(120)
        except PWError as exc:
            log.debug("scrollIntoView failed: %s", exc)

    async def _narrate(self, el: Element, verb: str) -> None:
        """Move the cursor and light up the target before acting on it."""
        x, y = el.point
        label = f"{verb} · {el.name[:40]}" if el.name else verb
        await self.ch.overlay.highlight(el.box, label)
        await self.ch.overlay.move_to(x, y)
        # Let the glide finish so the human sees where the click is going. The
        # duration mirrors the easing in overlay.js.
        await asyncio.sleep((self.ch.config.loop.think_aloud and 0.28) or 0.02)

    @staticmethod
    def _with_repairs(result: ActionResult, repairs: list[str]) -> ActionResult:
        result.repairs.extend(repairs)
        return result

    # -------------------------------------------------------------- navigation

    async def _do_navigate(self, a: S.Navigate) -> ActionResult:
        await self.ch.overlay.think(thought=f"Opening {a.url}", status="busy")
        response = await self.ch.page.goto(a.url, wait_until="domcontentloaded", timeout=45_000)
        ready = await self.ch.wait_ready()
        status = response.status if response else None
        if status and status >= 400:
            return ActionResult.failure(
                Outcome.NAVIGATION_FAILED,
                "navigate",
                f"{a.url} returned HTTP {status}.",
                status=status,
            )
        return ActionResult.success(
            "navigate", f"loaded {self.ch.page.url}", status=status, settled=ready.settled
        )

    async def _do_go_back(self, a: S.GoBack) -> ActionResult:
        response = await self.ch.page.go_back(wait_until="domcontentloaded", timeout=30_000)
        if response is None:
            return ActionResult.failure(
                Outcome.NAVIGATION_FAILED, "go_back", "No previous page in this tab's history."
            )
        await self.ch.wait_ready()
        return ActionResult.success("go_back", f"back at {self.ch.page.url}")

    async def _do_go_forward(self, a: S.GoForward) -> ActionResult:
        response = await self.ch.page.go_forward(wait_until="domcontentloaded", timeout=30_000)
        if response is None:
            return ActionResult.failure(
                Outcome.NAVIGATION_FAILED, "go_forward", "Nothing forward in this tab's history."
            )
        await self.ch.wait_ready()
        return ActionResult.success("go_forward", f"forward at {self.ch.page.url}")

    async def _do_reload(self, a: S.Reload) -> ActionResult:
        await self.ch.page.reload(wait_until="domcontentloaded", timeout=45_000)
        await self.ch.wait_ready()
        return ActionResult.success("reload", f"reloaded {self.ch.page.url}")

    # ------------------------------------------------------------------ pointer

    async def _do_click(self, a: S.Click) -> ActionResult:
        found = await self._target(a.ref, "click")
        if isinstance(found, ActionResult):
            return found
        el, res, repairs = found

        if res.occluded:
            return self._with_repairs(
                ActionResult.failure(
                    Outcome.OCCLUDED,
                    "click",
                    f"{serialize.describe_element(el)} is covered by "
                    f"{res.occluded_by or 'another element'}.",
                ),
                repairs,
            )

        await self._narrate(el, "click")
        x, y = el.point
        url_before = self.ch.page.url

        await self.ch.overlay.click_at(x, y)
        async with self.ch.expect_possible_popup() as popup:
            # Playwright's Mouse.click takes no modifiers argument — they are held
            # on the keyboard for the duration of the click. Ctrl+click is how a
            # model opens a link in a background tab, so this path matters.
            for mod in a.modifiers:
                await self.ch.page.keyboard.down(mod)
            try:
                await self.ch.page.mouse.click(x, y, button=a.button, click_count=a.clicks)
            finally:
                for mod in reversed(a.modifiers):
                    await self.ch.page.keyboard.up(mod)
            # Give a navigation or a handler a moment to start before we judge.
            await asyncio.sleep(0.18)

        detail = serialize.describe_element(el)
        if popup.page is not None:
            await self.ch.adopt_tab(popup.page, focus=True)
            return self._with_repairs(
                ActionResult.success("click", f"clicked {detail}; it opened a new tab", new_tab=True),
                repairs,
            )

        ready = await self.ch.wait_ready(timeout_ms=6000)
        if self.ch.page.url != url_before:
            return self._with_repairs(
                ActionResult.success("click", f"clicked {detail} → navigated to {self.ch.page.url}"),
                repairs,
            )
        return self._with_repairs(
            ActionResult.success("click", f"clicked {detail}", settled=ready.settled), repairs
        )

    async def _do_hover(self, a: S.Hover) -> ActionResult:
        found = await self._target(a.ref, "hover")
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found
        await self._narrate(el, "hover")
        await self.ch.page.mouse.move(*el.point)
        # Menus and tooltips need a beat to open.
        await asyncio.sleep(0.45)
        return self._with_repairs(
            ActionResult.success("hover", f"hovering {serialize.describe_element(el)}"), repairs
        )

    async def _do_drag(self, a: S.Drag) -> ActionResult:
        src = await self._target(a.from_ref, "drag")
        if isinstance(src, ActionResult):
            return src
        dst = await self._target(a.to_ref, "drag")
        if isinstance(dst, ActionResult):
            return dst
        el_a, el_b = src.el, dst.el

        await self._narrate(el_a, "drag from")
        await self.ch.page.mouse.move(*el_a.point)
        await self.ch.page.mouse.down()
        # Intermediate moves: HTML5 drag-and-drop and most JS libraries need more
        # than one mousemove to recognise a drag at all.
        ax, ay = el_a.point
        bx, by = el_b.point
        for i in range(1, 11):
            await self.ch.page.mouse.move(ax + (bx - ax) * i / 10, ay + (by - ay) * i / 10)
            await self.ch.overlay.move_to(ax + (bx - ax) * i / 10, ay + (by - ay) * i / 10, 20)
            await asyncio.sleep(0.02)
        await self.ch.page.mouse.up()
        await asyncio.sleep(0.2)
        return ActionResult.success(
            "drag", f"dragged {serialize.describe_element(el_a)} onto {serialize.describe_element(el_b)}"
        )

    # ----------------------------------------------------------------- keyboard

    async def _do_type_text(self, a: S.TypeText) -> ActionResult:
        found = await self._target(a.ref, "type_text")
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found

        if el.role not in ("textbox", "searchbox", "combobox", "spinbutton") and el.tag not in (
            "input",
            "textarea",
        ):
            return ActionResult.failure(
                Outcome.WRONG_ELEMENT,
                "type_text",
                f"{serialize.describe_element(el)} is a {el.role}, not a text field.",
            )

        await self._narrate(el, "type into")
        await self.ch.overlay.click_at(*el.point)
        await self.ch.page.mouse.click(*el.point)
        await asyncio.sleep(0.08)

        if a.clear:
            # Select-all + Delete rather than fill(): it goes through real key
            # events, which is what controlled React/Vue inputs listen for. fill()
            # sets .value directly and some frameworks never see it.
            await self.ch.page.keyboard.press("ControlOrMeta+a")
            await self.ch.page.keyboard.press("Delete")

        delay = min(_TYPE_DELAY_MS, max(1, _MAX_TYPE_TIME_MS // max(len(a.text), 1)))
        await self.ch.page.keyboard.type(a.text, delay=delay)

        if a.submit:
            await self.ch.page.keyboard.press("Enter")
            await self.ch.wait_ready(timeout_ms=8000)

        shown = a.text if len(a.text) <= 60 else a.text[:57] + "…"
        return self._with_repairs(
            ActionResult.success(
                "type_text",
                f'typed "{shown}" into {serialize.describe_element(el)}'
                + (" and submitted" if a.submit else ""),
            ),
            repairs,
        )

    async def _do_press_key(self, a: S.PressKey) -> ActionResult:
        if a.ref:
            found = await self._target(a.ref, "press_key")
            if isinstance(found, ActionResult):
                return found
            el = found.el
            await self._narrate(el, "focus")
            await self.ch.page.mouse.click(*el.point)
            await asyncio.sleep(0.06)

        await self.ch.page.keyboard.press(a.key)
        await self.ch.wait_ready(timeout_ms=5000)
        return ActionResult.success("press_key", f"pressed {a.key}")

    async def _do_select_option(self, a: S.SelectOption) -> ActionResult:
        found = await self._target(a.ref, "select_option")
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found
        await self._narrate(el, "select in")

        chosen = await self.ch.page.evaluate(
            """([ref, path, wanted]) => {
              let node = window.__chamber?.elements.get(ref);
              if (!node || !node.isConnected) {
                let scope = document; node = null;
                for (const hop of path || []) {
                  if (hop.kind === "shadow") { if (!node?.shadowRoot) return null; scope = node.shadowRoot; continue; }
                  node = scope.querySelector(hop.sel); if (!node) return null;
                }
              }
              if (!node || node.tagName !== "SELECT") return { error: "not-a-select" };
              const want = String(wanted).trim().toLowerCase();
              const opts = [...node.options];
              let hit = opts.find(o => o.textContent.trim().toLowerCase() === want)
                     || opts.find(o => (o.value || "").toLowerCase() === want)
                     || opts.find(o => o.textContent.trim().toLowerCase().includes(want));
              if (!hit) return { error: "no-option", available: opts.map(o => o.textContent.trim()).slice(0, 20) };
              node.value = hit.value;
              node.dispatchEvent(new Event("input", { bubbles: true }));
              node.dispatchEvent(new Event("change", { bubbles: true }));
              return { chosen: hit.textContent.trim() };
            }""",
            [el.ref, el.path, a.value],
        )

        if not chosen or chosen.get("error") == "not-a-select":
            return ActionResult.failure(
                Outcome.WRONG_ELEMENT,
                "select_option",
                f"{serialize.describe_element(el)} is not a <select>. If it is a custom "
                "dropdown, click it to open and then click the option.",
            )
        if chosen.get("error") == "no-option":
            options = ", ".join(chosen.get("available", [])[:12])
            return ActionResult.failure(
                Outcome.INVALID_ACTION,
                "select_option",
                f"No option matching {a.value!r}. Available: {options}",
            )

        await self.ch.wait_ready(timeout_ms=5000)
        return self._with_repairs(
            ActionResult.success("select_option", f"selected {chosen['chosen']!r}"), repairs
        )

    async def _do_upload_file(self, a: S.UploadFile) -> ActionResult:
        """Attach files to a file input.

        Existence is checked here rather than left to Playwright, because its error
        for a missing path is a long stack-shaped message and the model needs to know
        which path was wrong, not what threw.
        """
        missing = [p for p in a.paths if not Path(p).is_file()]
        if missing:
            return ActionResult.failure(
                Outcome.INVALID_ACTION,
                "upload_file",
                "no file at: " + ", ".join(missing)
                + ". Use a path you were given, exactly as it was given.",
            )

        found = await self._target(a.ref, "upload_file", need_point=False)
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found
        await self._narrate(el, "attach to")

        # `set_input_files` needs the input itself. Sites routinely hide the real
        # input behind a styled label, so if the ref landed on the wrapper, look
        # inside it before giving up.
        handle = await self.ch.page.evaluate_handle(
            """([ref, path]) => {
              let node = window.__chamber?.elements.get(ref);
              if (!node || !node.isConnected) node = document.querySelector(path);
              if (!node) return null;
              if (node.tagName === 'INPUT' && node.type === 'file') return node;
              return node.querySelector('input[type=file]')
                  || node.closest('label')?.querySelector('input[type=file]')
                  || null;
            }""",
            [el.ref, el.selector],
        )
        element = handle.as_element()
        if element is None:
            return ActionResult.failure(
                Outcome.WRONG_ELEMENT,
                "upload_file",
                f"{serialize.describe_element(el)} is not a file input and does not "
                "contain one. Look for a control whose role is 'file'.",
            )

        await element.set_input_files(a.paths)
        names = ", ".join(Path(p).name for p in a.paths)
        return self._with_repairs(
            ActionResult.success("upload_file", f"attached {names}"), repairs
        )

    # --------------------------------------------------------------- clipboard

    async def _do_copy(self, a: S.Copy) -> ActionResult:
        found = await self._target(a.ref, "copy", need_point=False)
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found

        text = await self.ch.page.evaluate(
            """([ref, path]) => {
              let node = window.__chamber?.elements.get(ref);
              if (!node || !node.isConnected) {
                let scope = document; node = null;
                for (const hop of path || []) {
                  if (hop.kind === "shadow") { if (!node?.shadowRoot) return null; scope = node.shadowRoot; continue; }
                  node = scope.querySelector(hop.sel); if (!node) return null;
                }
              }
              if (!node) return null;
              // Form fields carry their content in `value`; everything else in
              // rendered text — the same thing a human selecting it would get.
              if (node.value !== undefined && node.value !== null && node.value !== "") return node.value;
              const rendered = (node.innerText || node.textContent || "").trim();
              if (rendered) return rendered;

              // No inner text does not mean no content. Amazon's product cards
              // wrap the image in an <a> whose only label is an aria-label, so the
              // element the model can see and name has nothing to `innerText` —
              // copying it returned "" and looked like a broken element.
              const labelled =
                node.getAttribute?.("aria-label") ||
                node.getAttribute?.("title") ||
                node.getAttribute?.("alt") ||
                node.querySelector?.("img[alt]")?.getAttribute("alt") ||
                "";
              if (labelled.trim()) return labelled.trim();

              // Last resort: a link with neither text nor label still has a target.
              return (node.getAttribute?.("href") || "").trim();
            }""",
            [el.ref, el.path],
        )

        if not text:
            return ActionResult.failure(
                Outcome.WRONG_ELEMENT,
                "copy",
                f"{serialize.describe_element(el)} has no text to copy.",
            )

        clip = self.ch.clipboard.add(text, label=a.label, source_url=self.ch.page.url)
        await self._narrate_safely(action=f"copied {clip.preview(40)!r}")

        # Report a preview, not the text. The whole point is that the full content
        # never enters the model's context.
        name = f" as {a.label!r}" if a.label else ""
        return self._with_repairs(
            ActionResult.success(
                "copy",
                f"copied {len(text)} characters{name} (clip {len(self.ch.clipboard)}): "
                f"{clip.preview()!r}",
                chars=len(text),
                label=a.label,
            ),
            repairs,
        )

    async def _do_paste(self, a: S.Paste) -> ActionResult:
        if not self.ch.clipboard and not (a.prefix or a.suffix):
            return ActionResult.failure(
                Outcome.INVALID_ACTION,
                "paste",
                "The clipboard is empty — use `copy` on an element first.",
            )

        chosen, missing = self.ch.clipboard.select(a.labels)
        if missing:
            have = sorted({c.label for c in self.ch.clipboard if c.label})
            return ActionResult.failure(
                Outcome.INVALID_ACTION,
                "paste",
                f"No clip labelled {missing}. Labelled clips: {have or '(none)'}",
            )

        payload = f"{a.prefix}{self.ch.clipboard.render(chosen, a.separator)}{a.suffix}"

        found = await self._target(a.ref, "paste")
        if isinstance(found, ActionResult):
            return found
        el, _, repairs = found

        if el.role not in ("textbox", "searchbox", "combobox") and el.tag not in (
            "input",
            "textarea",
        ):
            return ActionResult.failure(
                Outcome.WRONG_ELEMENT,
                "paste",
                f"{serialize.describe_element(el)} is a {el.role}, not a text field.",
            )

        await self._narrate(el, "paste into")
        await self.ch.overlay.click_at(*el.point)
        await self.ch.page.mouse.click(*el.point)
        await asyncio.sleep(0.08)

        if a.clear:
            await self.ch.page.keyboard.press("ControlOrMeta+a")
            await self.ch.page.keyboard.press("Delete")

        # CDP's insertText is what Chrome itself uses for a paste: the field gets
        # proper beforeinput/input events, so controlled React inputs and rich-text
        # editors update, and a 2,000-character clip lands instantly instead of
        # taking half a minute at a human typing cadence.
        try:
            await self.ch.monitor.send("Input.insertText", {"text": payload})
        except Exception:
            log.debug("Input.insertText unavailable; typing instead")
            await self.ch.page.keyboard.type(payload, delay=1)

        if a.submit:
            await self.ch.page.keyboard.press("Enter")
            await self.ch.wait_ready(timeout_ms=15_000)

        return self._with_repairs(
            ActionResult.success(
                "paste",
                f"pasted {len(chosen)} clip(s), {len(payload)} characters into "
                f"{serialize.describe_element(el)}" + (" and submitted" if a.submit else ""),
                chars=len(payload),
            ),
            repairs,
        )

    async def _do_clipboard(self, a: S.Clipboard) -> ActionResult:
        if a.clear:
            return ActionResult.success(
                "clipboard", f"cleared {self.ch.clipboard.clear()} clip(s)"
            )
        if not self.ch.clipboard:
            return ActionResult.success("clipboard", "the clipboard is empty")
        return ActionResult.success(
            "clipboard",
            f"{len(self.ch.clipboard)} clip(s)",
            lines=self.ch.clipboard.summary(),
        )

    # ------------------------------------------------------- getting unstuck

    async def _do_dismiss_overlay(self, a: S.DismissOverlay) -> ActionResult:
        blocker = await find_blocker(self.ch.page)
        if not blocker.present:
            return ActionResult.success(
                "dismiss_overlay",
                "nothing is covering the page — carry on with what you were doing",
            )

        host = ""
        with contextlib.suppress(Exception):
            host = urlparse(self.ch.page.url).hostname or ""

        # A wall is not a nag. Dismissing is the wrong tool and saying so plainly
        # is more useful than a failed click.
        if blocker.is_wall:
            return ActionResult.failure(
                Outcome.CHALLENGE,
                "dismiss_overlay",
                f"{blocker.describe()}. There is no content behind it, so this is a "
                "wall rather than a nag — closing it is not the answer. Use "
                "ask_human (reason 'login' if it wants an account).",
            )

        seen = self.ch.nagging.record(host)
        gone, how = await dismiss(self.ch.page, blocker)

        if not gone:
            return ActionResult.failure(
                Outcome.NOT_INTERACTABLE,
                "dismiss_overlay",
                f"could not close {blocker.describe()} ({how}). Try scrolling, or "
                "ask_human if it will not go.",
            )

        # Snapshot refs were taken with the modal on top; they are stale now.
        self.ch.last_snapshot = None

        repairs: list[str] = []
        if self.ch.nagging.escalated(host):
            repairs.append(self.ch.nagging.advice(host))
        elif seen > 1:
            repairs.append(f"that is {seen} times on {host} — it will keep coming back")

        return self._with_repairs(
            ActionResult.success("dismiss_overlay", f"closed it — {how}"), repairs, repairs
        )

    # ------------------------------------------------------------------ scroll

    async def _do_scroll(self, a: S.Scroll) -> ActionResult:
        vp_h = self.ch.last_snapshot.viewport.h if self.ch.last_snapshot else 800
        amount = a.amount or int(vp_h * 0.85)

        if a.ref:
            found = await self._target(a.ref, "scroll", need_point=False)
            if isinstance(found, ActionResult):
                return found
            el = found.el
            moved = await self.ch.page.evaluate(
                """([ref, path, dir, amt]) => {
                  let node = window.__chamber?.elements.get(ref);
                  if (!node || !node.isConnected) {
                    let scope = document; node = null;
                    for (const hop of path || []) {
                      if (hop.kind === "shadow") { if (!node?.shadowRoot) return null; scope = node.shadowRoot; continue; }
                      node = scope.querySelector(hop.sel); if (!node) return null;
                    }
                  }
                  if (!node) return null;
                  const before = node.scrollTop;
                  if (dir === "top") node.scrollTop = 0;
                  else if (dir === "bottom") node.scrollTop = node.scrollHeight;
                  else node.scrollTop += (dir === "up" ? -amt : amt);
                  return { before, after: node.scrollTop, max: node.scrollHeight - node.clientHeight };
                }""",
                [el.ref, el.path, a.direction, amount],
            )
            if moved is None:
                return ActionResult.failure(Outcome.STALE_REF, "scroll", "That panel is gone.")
            if moved["before"] == moved["after"]:
                edge = "top" if moved["after"] == 0 else "bottom"
                return ActionResult.success(
                    "scroll", f"the panel is already at the {edge}", at_edge=True
                )
            return ActionResult.success("scroll", f"scrolled the panel to {moved['after']}px")

        before = await self.ch.page.evaluate("() => window.scrollY")
        if a.direction == "top":
            await self.ch.page.evaluate("() => scrollTo({ top: 0, behavior: 'instant' })")
        elif a.direction == "bottom":
            await self.ch.page.evaluate(
                "() => scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' })"
            )
        else:
            delta = -amount if a.direction == "up" else amount
            await self.ch.page.mouse.wheel(0, delta)
        await asyncio.sleep(0.25)
        after = await self.ch.page.evaluate("() => window.scrollY")

        if abs(after - before) < 4:
            edge = "top of the page" if after < 4 else "bottom of the page"
            return ActionResult.success(
                "scroll",
                f"nothing moved — already at the {edge}. "
                "If content is missing it may be in a scrollable panel instead.",
                at_edge=True,
            )

        moved = abs(after - before)
        message = f"scrolled to {int(after)}px"
        if a.amount and moved < vp_h * 0.15:
            # Observed repeatedly: the model passes amounts like 10 or 15, which
            # move the page imperceptibly and burn a step each time. Saying so is
            # better than silently obeying or second-guessing the number — it is
            # information the model cannot get from the page.
            message += (
                f" — that is only {int(moved / vp_h * 100)}% of the screen height, "
                "barely a nudge. Omit `amount` to scroll a full screen."
            )
        return ActionResult.success("scroll", message)

    async def _do_scroll_to(self, a: S.ScrollToRef) -> ActionResult:
        found = await self._target(a.ref, "scroll_to", need_point=False)
        if isinstance(found, ActionResult):
            return found
        el = found.el
        await self._scroll_into_view(el, block="center")
        return ActionResult.success("scroll_to", f"scrolled to {serialize.describe_element(el)}")

    # -------------------------------------------------------------------- tabs

    async def _do_open_tab(self, a: S.OpenTab) -> ActionResult:
        tab_id = await self.ch.open_tab(a.url or "about:blank", purpose=a.purpose)
        label = f" for {a.purpose!r}" if a.purpose else ""
        return ActionResult.success(
            "open_tab", f"opened tab {tab_id}{label}", tab_id=tab_id
        )

    async def _do_switch_tab(self, a: S.SwitchTab) -> ActionResult:
        if not await self.ch.switch_tab(a.tab_id):
            open_ids = ", ".join(self.ch.tab_ids())
            return ActionResult.failure(
                Outcome.INVALID_ACTION, "switch_tab", f"No tab {a.tab_id!r}. Open tabs: {open_ids}"
            )
        return ActionResult.success("switch_tab", f"now on tab {a.tab_id} ({self.ch.page.url})")

    async def _do_close_tab(self, a: S.CloseTab) -> ActionResult:
        tab_id = a.tab_id or self.ch.current_tab_id
        if not await self.ch.close_tab(tab_id):
            return ActionResult.failure(
                Outcome.INVALID_ACTION, "close_tab", f"Could not close tab {tab_id!r}."
            )
        return ActionResult.success("close_tab", f"closed {tab_id}; now on {self.ch.page.url}")

    # ----------------------------------------------------------------- waiting

    async def _do_wait_for(self, a: S.WaitFor) -> ActionResult:
        from chamber.browser import ready as ready_mod

        if a.text:
            found = await ready_mod.wait_for_text(self.ch.page, a.text, timeout_ms=a.timeout_ms)
            if not found:
                return ActionResult.failure(
                    Outcome.TIMEOUT,
                    "wait_for",
                    f"{a.text!r} did not appear within {a.timeout_ms}ms.",
                )
            return ActionResult.success("wait_for", f"{a.text!r} appeared")

        if a.selector:
            try:
                await self.ch.page.wait_for_selector(
                    a.selector, timeout=a.timeout_ms, state="visible"
                )
            except PWTimeout:
                return ActionResult.failure(
                    Outcome.TIMEOUT, "wait_for", f"{a.selector!r} never became visible."
                )
            return ActionResult.success("wait_for", f"{a.selector!r} is visible")

        ms = a.ms or 1000
        await asyncio.sleep(min(ms, 30_000) / 1000)
        return ActionResult.success("wait_for", f"waited {ms}ms")

    # -------------------------------------------------------------- inspection

    async def _do_read_page(self, a: S.ReadPage) -> ActionResult:
        from chamber.dom import reader

        html = await self.ch.page.content()
        result = reader.read(
            html,
            base_url=self.ch.page.url,
            budget=a.budget,
            whole_page=a.whole_page,
        )
        return ActionResult.success(
            "read_page",
            f"read {result.chars_out} chars from {result.main_selector}"
            + (" (truncated)" if result.truncated else ""),
            content=result.text,
            truncated=result.truncated,
        )

    async def _do_screenshot(self, a: S.Screenshot) -> ActionResult:
        # The description is the deliverable, not the file. A planner told only
        # "saved to shot-123.png" cannot see anything, so it takes another
        # screenshot, and another — a loop observed on a real Amazon run before
        # this path existed.
        path, description = await self.ch.look(a.question, full_page=a.full_page)
        return ActionResult.success(
            "screenshot", description, path=str(path), described=self.ch.vision is not None
        )

    async def _do_inspect(self, a: S.InspectElement) -> ActionResult:
        from chamber.browser import devtools

        snap = self.ch.last_snapshot
        el = snap.get(a.ref) if snap else None
        if el is None:
            return ActionResult.failure(
                Outcome.UNKNOWN_REF, "inspect", f"Ref {a.ref!r} is not in the control list."
            )
        info = await devtools.inspect_element(self.ch.page, a.ref, el.path)
        if info is None:
            return ActionResult.failure(Outcome.STALE_REF, "inspect", "The element is gone.")
        listeners = await devtools.listeners_via_cdp(self.ch.monitor, a.ref)
        if listeners:
            info["listeners"] = listeners
        await self.ch.overlay.highlight(el.box, f"inspect · {el.name[:36]}")
        return ActionResult.success("inspect", f"inspected {info['tag']}", **info)

    async def _do_console_log(self, a: S.ConsoleLog) -> ActionResult:
        entries = self.ch.monitor.console_log(limit=a.limit, errors_only=a.errors_only)
        if not entries:
            return ActionResult.success(
                "console_log", "the console is empty (nothing logged since this page loaded)"
            )
        return ActionResult.success(
            "console_log",
            f"{len(entries)} console entries",
            lines=[e.render() for e in entries],
        )

    async def _do_network_log(self, a: S.NetworkLog) -> ActionResult:
        entries = self.ch.monitor.network_log(
            limit=a.limit, url_contains=a.url_contains, failed_only=a.failed_only
        )
        if not entries:
            return ActionResult.success("network_log", "no matching requests")
        return ActionResult.success(
            "network_log",
            f"{len(entries)} requests",
            lines=[e.render() for e in entries],
        )

    async def _do_evaluate_js(self, a: S.EvaluateJS) -> ActionResult:
        try:
            value = await self.ch.page.evaluate(a.expression)
        except PWError as exc:
            return ActionResult.failure(
                Outcome.JS_ERROR, "evaluate_js", str(exc).split("\n")[0][:300]
            )
        return ActionResult.success("evaluate_js", "expression returned", value=_trim(value))

    # --------------------------------------------------------- human in the loop

    async def _do_ask_human(self, a: S.AskHuman) -> ActionResult:
        answer = await self.ch.ask_human(a.question, reason=a.reason, resume_when=a.resume_when)
        return ActionResult.success("ask_human", answer, reason=a.reason)

    async def _do_done(self, a: S.Done) -> ActionResult:
        await self.ch.overlay.think(
            thought=a.summary[:400], status="ok" if a.success else "warn", action="done"
        )
        return ActionResult.success("done", a.summary, success=a.success)


def _trim(value: Any, limit: int = 4000) -> Any:
    """Keep an evaluate_js result from flooding the context window."""
    if isinstance(value, str):
        return value[:limit] + ("… (truncated)" if len(value) > limit else "")
    if isinstance(value, list):
        return [_trim(v, 400) for v in value[:60]]
    if isinstance(value, dict):
        return {k: _trim(v, 400) for k, v in list(value.items())[:60]}
    return value
