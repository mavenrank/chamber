from __future__ import annotations

from pathlib import Path

from chamber.display import (
    DisplayAdapter,
    EventStatusGetter,
    FunctionStatusParser,
    StatusSignal,
    StatusUpdate,
)
from chamber.display.window import _app_html


def test_status_getter_parser_pair_slots_into_live_adapter():
    adapter = DisplayAdapter()

    def parse_custom(signal: StatusSignal) -> StatusUpdate:
        message = str(signal.payload.get("message", ""))
        return StatusUpdate(
            title="Syncing",
            detail=message,
            source=signal.source,
            tone="active",
            state_title="Syncing data",
            state_detail=message,
            state_tone="active",
        )

    adapter.register_status_adapter(
        EventStatusGetter("sync_progress", source="sync service"),
        FunctionStatusParser(parse_custom),
    )
    snapshot = adapter.handle("sync_progress", {"message": "12 records"})

    assert snapshot["current"] == {
        "title": "Syncing data",
        "detail": "12 records",
        "tone": "active",
    }
    assert snapshot["events"][-1]["source"] == "sync service"
    assert snapshot["events"][-1]["title"] == "Syncing"


def test_model_response_keeps_visible_summary_and_tool_calls():
    adapter = DisplayAdapter()
    snapshot = adapter.handle(
        "llm_response",
        {
            "role": "step",
            "model": "mimo-v2.5-free",
            "text": "I will inspect the results.",
            "reasoning_summary": "The results page has a clear next target.",
            "tool_calls": [{"name": "click", "arguments": {"ref": "e1"}}],
            "input_tokens": 20,
            "output_tokens": 8,
        },
    )

    entry = snapshot["events"][-1]
    assert entry["details"]["response"] == "I will inspect the results."
    assert entry["details"]["reasoning_summary"] == "The results page has a clear next target."
    assert entry["details"]["tool_calls"][0]["name"] == "click"
    assert snapshot["metrics"]["input_tokens"] == 20


def test_finished_state_keeps_the_current_panel_minimal():
    snapshot = DisplayAdapter().handle(
        "run_end",
        {
            "success": True,
            "summary": "A long final answer belongs in the activity chain, not the headline.",
            "steps": 7,
        },
    )

    assert snapshot["current"] == {
        "title": "Finished",
        "detail": "Task completed.",
        "tone": "success",
    }


def test_vision_event_embeds_a_small_screenshot(tmp_path: Path):
    image = tmp_path / "screen.png"
    # A valid 1x1 transparent PNG, kept inline so the test has no image dependency.
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
        )
    )
    snapshot = DisplayAdapter().handle(
        "vision",
        {"screenshot": str(image), "question": "What is visible?", "text": "A blank screen."},
    )

    event = snapshot["events"][-1]
    assert event["screenshot"].startswith("data:image/png;base64,")
    assert event["screenshot_path"] == str(image)


def test_bundled_react_app_is_inlineable_for_the_app_window():
    html = _app_html()
    assert "__chamberDesk" in html
    assert "chamber-shimmer" in html
    assert "current-step-display" in html
    assert "step-group" in html
    assert "step-marker-circle" in html
    assert "state-step-badge" not in html
    assert "entry-step" not in html
    assert "scroll-area-scrollbar" in html
    assert 'src="/desk.js"' not in html
    assert 'href="/desk.css"' not in html
