"""Run an interactive Chamber Desk fixture without calling an LLM or a website.

This deliberately exercises the normalized display event path so the UI can be
inspected with deterministic data:

    uv run python demos/display_fixture.py --screenshot C:\\path\\to\\screen.png

The window remains open after the fixture finishes. Close the Chamber Desk window
or press Ctrl+C in the terminal to stop it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import async_playwright

from chamber.browser.discovery import discover_browser
from chamber.display import ChamberDesk

MAX_STEPS = 18


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--screenshot",
        type=Path,
        help="an existing screenshot to embed in the vision entries",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="seconds between fixture events (default: 1.0)",
    )
    return parser


def _events(screenshot: str) -> list[tuple[str, dict[str, object]]]:
    request = {
        "system": "You are operating a browser and must report only model-visible notes.",
        "messages": [
            {"role": "user", "content": "Inspect the current page and find the next target."},
            {"role": "assistant", "content": "I will inspect the visible controls."},
        ],
        "tools": [
            {"name": "click", "description": "Click a visible browser control."},
            {"name": "scroll", "description": "Scroll the current page."},
        ],
    }
    return [
        (
            "run_start",
            {
                "task": "Inspect a multi-source page and verify the evidence trail",
                "step": 0,
                "max_steps": MAX_STEPS,
            },
        ),
        ("vision_ready", {"loaded": True, "seconds": 0.42}),
        (
            "observe",
            {
                "step": 1,
                "max_steps": MAX_STEPS,
                "url": "https://example.com/research/overview",
                "controls": 14,
                "tabs": [
                    {"id": "overview", "url": "https://example.com/research/overview", "title": "Overview"},
                    {"id": "source-a", "url": "https://example.com/source-a", "title": "Source A"},
                ],
            },
        ),
        (
            "plan",
            {
                "step": 2,
                "max_steps": MAX_STEPS,
                "stage": "Compare the primary sources",
                "current": 1,
                "stages": ["Inspect", "Compare", "Verify", "Summarize"],
                "assessment": "The overview exposes two source links and one expandable evidence panel.",
            },
        ),
        (
            "llm_request",
            {
                "step": 2,
                "max_steps": MAX_STEPS,
                "role": "planner",
                "model": "fixture-planner",
                "messages": 8,
                "tools": 2,
                "prompt_chars": 1840,
                "last_user": "Find the strongest evidence and record where it came from.",
                "request": request,
            },
        ),
        (
            "thought",
            {
                "step": 2,
                "max_steps": MAX_STEPS,
                "source": "planner",
                "text": "The primary source should be opened before relying on the summary page.",
            },
        ),
        (
            "llm_response",
            {
                "step": 2,
                "max_steps": MAX_STEPS,
                "role": "planner",
                "model": "fixture-planner",
                "text": "Open source A, then compare its claim with source B.",
                "reasoning_summary": "The visible page structure gives us a reliable order of operations.",
                "tool_calls": [{"name": "click", "arguments": {"ref": "source-a"}}],
                "input_tokens": 442,
                "output_tokens": 76,
                "seconds": 0.81,
                "stop_reason": "tool_calls",
            },
        ),
        (
            "action",
            {
                "step": 3,
                "max_steps": MAX_STEPS,
                "name": "click",
                "detail": "Clicked the Source A link.",
                "ok": True,
            },
        ),
        (
            "observe",
            {
                "step": 4,
                "max_steps": MAX_STEPS,
                "url": "https://example.com/source-a",
                "controls": 9,
                "tabs": [{"id": "source-a", "url": "https://example.com/source-a", "title": "Source A"}],
            },
        ),
        (
            "llm_request",
            {
                "step": 5,
                "max_steps": MAX_STEPS,
                "role": "step",
                "model": "fixture-step-model",
                "messages": 11,
                "tools": 3,
                "images": 1,
                "prompt_chars": 2310,
                "last_user": "Read the screenshot and select the evidence panel.",
            },
        ),
        (
            "vision_request",
            {
                "step": 5,
                "max_steps": MAX_STEPS,
                "screenshot": screenshot,
                "question": "Which visible control expands the evidence panel?",
            },
        ),
        (
            "vision",
            {
                "step": 5,
                "max_steps": MAX_STEPS,
                "screenshot": screenshot,
                "question": "Which visible control expands the evidence panel?",
                "text": "The evidence panel is the control labelled More details near the lower right.",
            },
        ),
        (
            "screenshot",
            {
                "step": 6,
                "max_steps": MAX_STEPS,
                "screenshot": screenshot,
                "full_page": False,
                "scale": 1,
            },
        ),
        (
            "llm_response",
            {
                "step": 6,
                "max_steps": MAX_STEPS,
                "role": "step",
                "model": "fixture-step-model",
                "text": "The screenshot confirms the More details control is visible.",
                "reasoning_summary": "The visual evidence resolves the target without guessing from DOM order.",
                "tool_calls": [{"name": "click", "arguments": {"label": "More details"}}],
                "input_tokens": 1180,
                "output_tokens": 92,
                "seconds": 1.24,
                "stop_reason": "tool_calls",
            },
        ),
        (
            "action",
            {
                "step": 7,
                "max_steps": MAX_STEPS,
                "name": "click",
                "detail": "Expanded the evidence panel.",
                "ok": True,
            },
        ),
        (
            "controlled",
            {"step": 8, "max_steps": MAX_STEPS, "taken": True, "seconds": 0},
        ),
        (
            "human_request",
            {
                "step": 8,
                "max_steps": MAX_STEPS,
                "heading": "Quick confirmation",
                "question": "Confirm the expanded panel is the source you want to compare.",
                "reason": "The fixture is exercising a human handoff.",
                "resume_when": "you confirm the panel",
            },
        ),
        (
            "human_resolved",
            {
                "step": 8,
                "max_steps": MAX_STEPS,
                "resolved": True,
                "reason": "Panel confirmed for the fixture.",
                "waited_s": 2.3,
            },
        ),
        (
            "stuck",
            {
                "step": 9,
                "max_steps": MAX_STEPS,
                "count": 2,
                "vision": True,
            },
        ),
        (
            "parse_retry",
            {
                "step": 10,
                "max_steps": MAX_STEPS,
                "attempt": 1,
                "error": "The action block was missing its target reference.",
            },
        ),
        (
            "llm_request",
            {
                "step": 11,
                "max_steps": MAX_STEPS,
                "role": "step",
                "model": "fixture-step-model",
                "messages": 15,
                "tools": 3,
                "images": 0,
                "prompt_chars": 2680,
                "last_user": "Retry with the exact visible target.",
            },
        ),
        (
            "llm_response",
            {
                "step": 11,
                "max_steps": MAX_STEPS,
                "role": "step",
                "model": "fixture-step-model",
                "text": "I will use the exact visible label and verify the result afterward.",
                "reasoning_summary": "The repaired response contains a concrete target and a verification step.",
                "tool_calls": [{"name": "click", "arguments": {"label": "More details"}}],
                "input_tokens": 920,
                "output_tokens": 68,
                "seconds": 0.73,
                "stop_reason": "tool_calls",
            },
        ),
        (
            "action",
            {
                "step": 12,
                "max_steps": MAX_STEPS,
                "name": "scroll",
                "detail": "The target moved before the scroll completed.",
                "ok": False,
            },
        ),
        (
            "action",
            {
                "step": 13,
                "max_steps": MAX_STEPS,
                "name": "scroll",
                "detail": "Scrolled to the evidence section.",
                "ok": True,
            },
        ),
        (
            "observe",
            {
                "step": 14,
                "max_steps": MAX_STEPS,
                "url": "https://example.com/source-a#evidence",
                "controls": 12,
                "tabs": [{"id": "source-a", "url": "https://example.com/source-a#evidence", "title": "Source A evidence"}],
            },
        ),
        (
            "plan",
            {
                "step": 15,
                "max_steps": MAX_STEPS,
                "stage": "Verify the cited passage",
                "current": 3,
                "stages": ["Inspect", "Compare", "Verify", "Summarize"],
                "assessment": "The cited passage is visible and can be matched against source B.",
            },
        ),
        (
            "thought",
            {
                "step": 15,
                "max_steps": MAX_STEPS,
                "source": "step model",
                "text": "The cited passage matches the source title and the surrounding context.",
            },
        ),
        (
            "llm_response",
            {
                "step": 16,
                "max_steps": MAX_STEPS,
                "role": "step",
                "model": "fixture-step-model",
                "text": "The evidence is verified; I can finish with the source trail attached.",
                "reasoning_summary": "Both sources now have visible evidence in the activity chain.",
                "tool_calls": [],
                "input_tokens": 640,
                "output_tokens": 54,
                "seconds": 0.51,
                "stop_reason": "stop",
            },
        ),
        (
            "vision_ready",
            {"step": 17, "max_steps": MAX_STEPS, "loaded": False, "seconds": 0},
        ),
        (
            "action",
            {
                "step": 17,
                "max_steps": MAX_STEPS,
                "name": "record_evidence",
                "detail": "Recorded the verified passage and its source URL.",
                "ok": True,
            },
        ),
        (
            "run_end",
            {
                "step": MAX_STEPS,
                "max_steps": MAX_STEPS,
                "success": True,
                "steps": MAX_STEPS,
                "via_tools": 7,
                "via_text": 0,
                "tool_rate": 1.0,
                "summary": "Verified the evidence trail across both sources.",
            },
        ),
    ]


async def main() -> int:
    args = _parser().parse_args()
    pace = max(0.15, args.pace)
    screenshot = ""
    if args.screenshot:
        image = args.screenshot.expanduser().resolve()
        if image.is_file():
            screenshot = str(image)
        else:
            print(f"warning: screenshot does not exist, continuing without it: {image}")

    build = discover_browser()
    print(f"Using {build.label}")
    print("Starting Chamber Desk fixture. Close its window or press Ctrl+C to stop.")

    desk: ChamberDesk | None = None

    async def on_action(action: str) -> None:
        if desk is None or not desk.started:
            return
        if action == "take_control":
            desk.handle("controlled", {"taken": True, "source": "display"})
        elif action == "give_control":
            desk.handle("controlled", {"taken": False, "source": "display"})

    async with async_playwright() as pw:
        desk = ChamberDesk(
            pw,
            build,
            width=520,
            height=780,
            headless=False,
            on_action=on_action,
        )
        if not await desk.start():
            print("Could not start Chamber Desk.")
            return 1

        try:
            await asyncio.sleep(max(1.0, pace))
            for event, payload in _events(screenshot):
                if not desk.started:
                    print("Chamber Desk was closed; ending fixture.")
                    break
                desk.handle(event, payload)
                print(f"  {event:16} step={payload.get('step', '-')}" )
                await asyncio.sleep(pace)
                if event == "human_request":
                    await asyncio.sleep(max(2.0, pace))

            if desk.started:
                print("Fixture complete. The window will stay open for inspection.")
                while desk.started:
                    await asyncio.sleep(0.5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("Stopping Chamber Desk fixture.")
        finally:
            await desk.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
