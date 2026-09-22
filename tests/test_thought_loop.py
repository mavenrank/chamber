"""ThoughtLoop join: thoughts + exchanges + actions + planner turns."""

from __future__ import annotations

import json

import pytest

from chamber.display import queries as q


def _request(text):
    return {
        "system": "sys",
        "messages": [{"role": "user", "content": text, "images": 0}],
        "tools": [],
    }


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    from chamber.trace.store import open_store

    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    run = "20260921-000000-abc123"
    big_prompt = "# Step 1 of 3\n" + "x" * 100_000
    plan_text = json.dumps(
        {
            "assessment": "good",
            "stages": ["Inspect", "Compare"],
            "current": 1,
            "notes": "n1",
            "done": False,
        }
    )
    with open_store() as store:
        store.start_run(run, "task", profile="default", model="m")
        store.record_step(1, thought="look around", url="https://example.com", ms=100)
        store.record_step(2, thought="keep going", url="https://example.com", ms=100)
        store.record_action(1, 0, name="click", args={"ref": "e1"}, why="looks right",
                            outcome="ok", message="clicked")
        store.record_model_event("llm_request", {
            "exchange_id": "x-step", "role": "step", "model": "m",
            "request": _request(big_prompt),
        })
        store.record_model_event("llm_response", {
            "exchange_id": "x-step", "role": "step", "model": "m",
            "text": "I will click", "reasoning_summary": "rs",
            "tool_calls": [{"name": "click", "arguments": {"ref": "e1"}}],
            "input_tokens": 10, "output_tokens": 5, "seconds": 0.5,
        })
        store.record_model_event("llm_request", {
            "exchange_id": "x-plan", "role": "planner", "model": "p",
            "request": _request("Step 1 of 3. Currently on https://example.com"),
        })
        store.record_model_event("llm_response", {
            "exchange_id": "x-plan", "role": "planner", "model": "p",
            "text": "plan says: " + plan_text,
            "input_tokens": 5, "output_tokens": 5, "seconds": 0.5,
        })
    return run


def test_loop_joins_all_parts(seeded):
    loop = q.thought_loop(seeded)
    assert loop["total_steps"] == 2
    assert len(loop["steps"]) == 2
    one = loop["steps"][0]
    assert one["thought"] == "look around"
    assert one["exchange"]["model"] == "m"
    assert one["exchange"]["request"]["last_user_truncated"] is True
    assert one["exchange"]["reasoning_summary"] == "rs"
    assert one["actions"][0]["why"] == "looks right"
    assert one["planner_turn"]["plan"]["current"] == 1
    assert one["active_stage"] == {"stage": "Compare", "current": 1, "total": 2}
    two = loop["steps"][1]
    assert two["exchange"] is None  # no exchange parsed to step 2
    assert two["active_stage"]["stage"] == "Compare"  # plan carries forward


def test_incremental_since_step(seeded):
    loop = q.thought_loop(seeded, since_step=2)
    assert [s["n"] for s in loop["steps"]] == [2]
    assert loop["total_steps"] == 2


def test_payload_ceiling(seeded):
    payload = json.dumps(q.thought_loop(seeded), default=str)
    assert len(payload) < 100_000  # 100KB prompt ships as excerpt, not body


def test_exchange_detail_full_body(seeded):
    got = q.exchange_detail(seeded, "x-step")
    assert got["exchange"]["role"] == "step"
    assert got["exchange"]["request_json_truncated"] is False
    assert "# Step 1 of 3" in got["exchange"]["request_json"]
    missing = q.exchange_detail(seeded, "nope")
    assert missing["exchange"] is None
    assert q.thought_loop("does-not-exist")["run"] is None
