"""End-to-end check of the browser half, with no model involved.

    uv run python scripts/smoke.py

Opens a real window and exercises the pieces that are hard to unit-test: launching
with the extension, injecting the overlay, extracting a page, resolving a ref, and
executing a click that navigates. If this passes, any failure above it is in the
model layer, not the browser layer.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chamber import Chamber, ChamberConfig
from chamber.config import BrowserConfig, LoopConfig

TARGET = "https://example.com/"


def show(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


async def main() -> int:
    cfg = ChamberConfig(
        browser=BrowserConfig(profile="smoke", viewport=(1280, 860)),
        loop=LoopConfig(think_aloud=True),
    )
    passed = True

    async with Chamber.open(cfg) as ch:
        print(f"\nbrowser: {ch.build.label}  (MV2 capable: {ch.build.mv2})")

        exts = await ch.extension_status()
        if exts:
            for e in exts:
                state = "enabled" if e.get("enabled") else "DISABLED"
                print(f"extension: {e.get('name')} {e.get('version') or ''} [{state}]")
            passed &= show(
                "uBlock Origin loaded and enabled",
                any("ublock" in (e.get("name") or "").lower() and e.get("enabled") for e in exts),
            )
        else:
            print("extension: none reported")

        print("\n--- navigate ---")
        result = await ch.goto(TARGET)
        passed &= show("navigate", result.ok, result.message)

        print("\n--- observe ---")
        snap = await ch.observe()
        passed &= show("page title", bool(snap.title), snap.title)
        passed &= show("controls found", len(snap.elements) > 0, f"{len(snap.elements)} elements")
        passed &= show("content extracted", len(snap.content) > 50, f"{len(snap.content)} chars")
        print(f"  extract stats: {snap.stats}")

        print("\n--- what the model would see ---")
        rendered = ch.render()
        print("\n".join("  | " + line for line in rendered.splitlines()[:28]))
        print(f"  | … ({len(rendered)} chars total)")

        print("\n--- overlay ---")
        await ch.overlay.think(
            goal="smoke test", thought="Checking that the HUD renders and the cursor moves.",
            step="1/3", status="busy",
        )
        state = await ch.overlay.cursor_state()
        passed &= show("overlay installed", state is not None, str(state))
        await ch.overlay.move_to(300, 300)
        await asyncio.sleep(0.5)
        await ch.overlay.move_to(900, 500)
        await asyncio.sleep(0.5)

        print("\n--- act: click a link ---")
        links = [e for e in snap.elements if e.role == "link"]
        if links:
            target = links[0]
            print(f"  targeting [{target.ref}] {target.name!r} → {target.href}")
            result = await ch.act({"action": "click", "ref": target.ref, "why": "smoke test"})
            passed &= show("click", result.ok, result.message or result.for_model())
            await ch.observe()
            print(f"  now at: {ch.page.url}")
        else:
            print("  (no links on the page to click)")

        print("\n--- act: rejected input ---")
        bad = await ch.act({"action": "click", "ref": "e9999"})
        passed &= show("unknown ref rejected cleanly", not bad.ok and bad.outcome == "unknown_ref")
        print("\n".join("  | " + line for line in bad.for_model().splitlines()))

        malformed = await ch.act({"action": "click", "index": 42})
        passed &= show("malformed action rejected cleanly", not malformed.ok)
        print("\n".join("  | " + line for line in malformed.for_model().splitlines()))

        print("\n--- extraction traps ---")
        # A synthetic page reproducing two bugs found against real sites. Both were
        # silent: the element was present, visible by every CSS measure, and simply
        # never reached the model.
        trap_html = """<!doctype html><meta charset="utf-8"><title>traps</title><body>
          <div style="display:contents">
            <div style="display:contents">
              <div contenteditable="true" aria-label="Composer"><p>hi</p></div>
              <button>Deep button</button>
            </div>
          </div>
          <!-- A genuine 1x1: border and padding zeroed, or the browser's default
               input chrome makes the border box ~9px and it is legitimately
               clickable. This mirrors the real ChatGPT case, box=(599,369,1,1). -->
          <input aria-label="Hidden one-pixel input"
                 style="width:1px;height:1px;border:0;padding:0;box-sizing:border-box">
          <button style="display:none">Never rendered</button>
          <div style="visibility:hidden">
            <button style="visibility:visible">Re-shown child</button>
          </div>
        </body>"""
        trap_file = Path(tempfile.gettempdir()) / "chamber-extraction-traps.html"
        trap_file.write_text(trap_html, encoding="utf-8")
        result = await ch.goto(trap_file.as_uri())
        passed &= show("trap page loaded", result.ok, result.message)
        snap = await ch.observe()
        names = {e.name for e in snap.elements}
        roles = {e.name: e.role for e in snap.elements}

        # display:contents generates no box but still renders its children.
        passed &= show(
            "descends through display:contents",
            "Deep button" in names,
            f"found {sorted(names)}",
        )
        passed &= show("contenteditable is a textbox", roles.get("Composer") == "textbox")
        passed &= show(
            "nested <p> inside it is not a second textbox",
            sum(1 for e in snap.elements if e.role == "textbox") == 1,
        )
        passed &= show(
            "1x1 inputs are filtered out", "Hidden one-pixel input" not in names
        )
        passed &= show("display:none is skipped", "Never rendered" not in names)
        passed &= show(
            "visibility:visible child of a hidden parent is kept",
            "Re-shown child" in names,
        )

        composer = next((e for e in snap.elements if e.name == "Composer"), None)
        if composer:
            typed = await ch.act(
                {"action": "type_text", "ref": composer.ref, "text": "typed into a div"}
            )
            landed = await ch.page.evaluate(
                "() => document.querySelector('[contenteditable]')?.innerText.trim()"
            )
            passed &= show(
                "typing into a contenteditable lands", typed.ok and landed == "typed into a div",
                repr(landed),
            )

        print("\n--- devtools ---")
        console = await ch.act({"action": "console_log", "limit": 5})
        passed &= show("console_log", console.ok, console.message)
        network = await ch.act({"action": "network_log", "limit": 5})
        passed &= show("network_log", network.ok, network.message)

        await ch.overlay.think(
            thought="Smoke test finished." if passed else "Smoke test had failures.",
            status="ok" if passed else "err",
        )
        print("\nholding the window open for 3s so you can see the overlay…")
        await asyncio.sleep(3)

    print("\n" + ("SMOKE PASSED" if passed else "SMOKE FAILED"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
