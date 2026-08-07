"""Snapshot → the text the model actually reads.

This file is the whole interface between "what the browser knows" and "what the
model knows", so its formatting choices are behaviour, not cosmetics:

* **Controls and content are separate sections.** The model acts on the first and
  reasons over the second. Interleaving them, which a raw accessibility dump does,
  makes both harder to use and doubles the length.

* **Off-screen controls go in their own section.** A model shown one flat list has
  no way to know that clicking item 40 requires scrolling first, so it clicks, the
  click lands somewhere else, and the step is wasted. Splitting the list makes the
  scroll requirement impossible to miss.

* **Problems are stated inline, next to the thing they affect.** `⚠ covered by
  div#cookie-banner` on the element the model wants to click leads to dismissing
  the banner. The same fact in a footnote leads to clicking anyway.

* **Nothing is silently dropped.** Truncation and element budgets are always
  announced, because an agent that believes it has seen the whole page will
  confidently report that something is not there.
"""

from __future__ import annotations

from chamber.dom.model import Element, Snapshot

_ROLE_WIDTH = 9


def _fmt_element(el: Element) -> str:
    bits: list[str] = [f"[{el.ref}]", f"{el.role:<{_ROLE_WIDTH}}"]

    name = el.name or "(no label)"
    bits.append(f'"{name}"')

    # Size is the difference between a result headline and the favicon beside it.
    # On a DuckDuckGo results page the article title is a 628×26 link and the
    # "Search domain dev.to" icon next to it is 32×32 — both are links, both are
    # sensibly labelled, and without this marker a model picks the icon and lands
    # on a filtered search instead of the article. Marked rather than hidden: a
    # close button is 32×32 too, and sometimes the icon is exactly what you want.
    if el.is_icon:
        bits.append("(icon)")

    if el.input_type and el.input_type not in ("text", "submit", "button"):
        bits.append(f"type={el.input_type}")
    if el.value:
        bits.append(f'value="{el.value}"')
    elif el.placeholder and el.placeholder != el.name:
        bits.append(f'placeholder="{el.placeholder}"')
    if el.checked is not None:
        bits.append("checked" if el.checked else "unchecked")
    if el.expanded is not None:
        bits.append("expanded" if el.expanded else "collapsed")
    if el.options:
        shown = ", ".join(el.options[:6])
        more = f" +{len(el.options) - 6}" if len(el.options) > 6 else ""
        bits.append(f"options=[{shown}{more}]")
        if isinstance(el.selected, str) and el.selected:
            bits.append(f'selected="{el.selected}"')
    if el.href:
        bits.append(f"→ {el.href}")
    if el.new_tab:
        bits.append("(opens new tab)")
    if el.required:
        bits.append("required")
    if el.focused:
        bits.append("[focused]")
    if el.disabled:
        bits.append("⊘ DISABLED")
    if el.occluded:
        by = f" by {el.occluded_by}" if el.occluded_by else ""
        bits.append(f"⚠ covered{by}")

    return "  ".join(bits)


def _viewport_line(snap: Snapshot) -> str:
    vp = snap.viewport
    parts = [f"{vp.w}×{vp.h}"]
    if vp.doc_h > vp.h:
        pct = int(vp.scroll_progress * 100)
        remaining = max(vp.doc_h - vp.h - vp.scroll_y, 0)
        parts.append(f"scrolled {pct}%")
        if remaining > 0:
            parts.append(f"{remaining}px more below")
        else:
            parts.append("at bottom")
    else:
        parts.append("whole page fits")
    return " · ".join(parts)


def render(
    snap: Snapshot,
    *,
    include_content: bool = True,
    max_controls: int = 80,
) -> str:
    """The observation block handed to the model each step."""
    out: list[str] = []

    # --- identity -----------------------------------------------------------
    out.append("## Page")
    if snap.title:
        out.append(snap.title)
    out.append(snap.url)
    out.append(_viewport_line(snap))
    if snap.ready_state != "complete":
        out.append(f"⏳ document.readyState = {snap.ready_state} (still loading)")

    if len(snap.open_tabs) > 1:
        out.append("")
        out.append(f"## Tabs ({len(snap.open_tabs)} open)")
        out.append("Switch between these instead of re-navigating — a tab you leave open")
        out.append("is state you do not have to remember.")
        for tab in snap.open_tabs:
            marker = "▸" if tab.get("id") == snap.tab_id else " "
            purpose = f"  ({tab['purpose']})" if tab.get("purpose") else ""
            label = tab.get("title", "")[:50] or tab.get("url", "")[:70]
            out.append(f" {marker} [{tab.get('id')}] {label}{purpose}")
            if tab.get("title"):
                out.append(f"      {tab.get('url', '')[:90]}")

    # --- notices ------------------------------------------------------------
    if snap.notices:
        out.append("")
        out.append("## Notices")
        out.extend(f"! {n}" for n in snap.notices)

    # --- controls -----------------------------------------------------------
    onscreen = [e for e in snap.elements if e.in_viewport]
    offscreen = [e for e in snap.elements if not e.in_viewport]

    out.append("")
    out.append(f"## Controls on screen ({len(onscreen)})")
    if onscreen:
        out.append("Act on these by ref, e.g. {\"action\": \"click\", \"ref\": \"" + onscreen[0].ref + "\"}")
        for el in onscreen[:max_controls]:
            out.append("  " + _fmt_element(el))
        if len(onscreen) > max_controls:
            out.append(f"  … {len(onscreen) - max_controls} more on screen, not listed")
    else:
        out.append("  (none — the page may still be rendering, or content is in an iframe)")

    if offscreen:
        # Off-screen controls are context, not the working set — the model has to
        # scroll before it can touch any of them. A short sample tells it what is
        # down there without paying for the full list.
        shown = offscreen[: max(12, max_controls // 6)]
        out.append("")
        out.append(f"## Controls off screen ({len(offscreen)}) — scroll to reach these")
        for el in shown:
            out.append("  " + _fmt_element(el))
        if len(offscreen) > len(shown):
            out.append(f"  … {len(offscreen) - len(shown)} more")

    # --- scroll containers --------------------------------------------------
    if snap.scrollables:
        out.append("")
        out.append("## Scrollable panels")
        out.append("These scroll independently of the window; scroll them with a ref.")
        for s in snap.scrollables:
            out.append(f'  "{s.label}"  {int(s.progress * 100)}% through {s.scroll_height}px')

    # --- readable content ---------------------------------------------------
    if include_content and snap.content:
        out.append("")
        out.append("## Page content")
        out.append(snap.content)

    # --- budget honesty -----------------------------------------------------
    dropped = snap.stats.get("dropped", 0)
    if dropped:
        out.append("")
        out.append(
            f"_Note: {dropped} lower-priority controls were omitted to fit the budget. "
            f"If what you need is missing, scroll or say so and the budget will be raised._"
        )

    return "\n".join(out)


def render_compact(snap: Snapshot) -> str:
    """A one-screen summary — for logs, the HUD, and stuck-detection diffing."""
    onscreen = sum(1 for e in snap.elements if e.in_viewport)
    return (
        f"{snap.title[:60] or '(untitled)'} | {snap.url[:80]} | "
        f"{onscreen}/{len(snap.elements)} controls on screen | "
        f"{len(snap.content)} chars content"
    )


def fingerprint(snap: Snapshot) -> str:
    """A cheap identity for "did anything actually change?".

    Used by stuck-detection. Deliberately coarse — it keys on URL, the set of
    control labels and the scroll position, so a spinner animating or an ad
    rotating does not read as progress, but a new dialog or a changed list does.
    """
    labels = "|".join(f"{e.role}:{e.name[:30]}" for e in snap.elements[:60])
    return f"{snap.url}#{snap.viewport.scroll_y // 100}#{hash(labels) & 0xFFFFFFFF:08x}"


def describe_element(el: Element) -> str:
    """Human-readable one-liner, for HUD labels and trace logs."""
    label = el.name or el.placeholder or el.href or el.tag
    return f"{el.role} “{label[:50]}”"
