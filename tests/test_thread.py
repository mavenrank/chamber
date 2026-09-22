"""Thread model: parent links, recorded notes, restart context, stable prompts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from chamber.display import queries as q
from chamber.trace.store import open_store


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    yield tmp_path


def test_parent_migration_and_link(home):
    target = home / ".chamber" / "chamber.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.execute(
        "CREATE TABLE run (id TEXT PRIMARY KEY, task TEXT NOT NULL, profile TEXT NOT NULL,"
        " model TEXT, started_at REAL NOT NULL, ended_at REAL, success INTEGER,"
        " summary TEXT, stopped_because TEXT,"
        " input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,"
        " last_beat REAL)"
    )
    conn.commit()
    conn.close()

    with open_store() as store:
        store.start_run("child", "task", profile="default", model="m", parent="mom")
        assert store.run("child")["parent_run"] == "mom"


def test_notes_recorded_and_read(home):
    with open_store() as store:
        store.start_run("r1", "task", profile="default", model="m")
        store.record_note(3, "go slower")
        store.record_note(None, "context: earlier work")
    with open_store() as store:
        notes = store.notes("r1")
    assert [(n["step_n"], n["text"]) for n in notes] == [(3, "go slower"), (None, "context: earlier work")]


def test_restart_context_shape(home):
    from chamber.trace.store import open_store as _open

    plan = json.dumps({"assessment": "a", "stages": ["S1", "S2"], "current": 0,
                       "notes": "found prices", "done": False})
    with _open() as store:
        store.start_run("old", "buy a mouse", profile="default", model="m")
        store.record_visit("https://example.com/x", title="X")
        store.mark_used(["https://example.com/x"])
        store.record_note(2, "prefer wired")
        store.record_model_event("llm_request", {
            "exchange_id": "p1", "role": "planner", "model": "p",
            "request": {"system": "", "messages": [], "tools": []},
        })
        store.record_model_event("llm_response", {
            "exchange_id": "p1", "role": "planner", "model": "p", "text": plan,
            "input_tokens": 1, "output_tokens": 1, "seconds": 0.1,
        })
    ctx = q.restart_context("old")
    assert ctx["task"] == "buy a mouse"
    assert "found prices" in ctx["context_note"]
    assert "https://example.com/x" in ctx["context_note"]
    assert "prefer wired" in ctx["context_note"]
    assert len(ctx["context_note"]) <= 3500
    assert q.restart_context("missing")["run"] is None
    assert q.restart_context("../evil")["run"] is None


def test_mailbox_claim_order_chronological(home):
    from chamber import inbox

    inbox.post_note("r1", "context first")
    inbox.post_note("r1", "steer second")
    claimed = inbox.claim_notes("r1")
    assert [n["text"] for n in claimed] == ["context first", "steer second"]


def test_system_prompt_stable_across_steps():
    from chamber.agent import prompt

    first = prompt.system_prompt(tool_calling=True)
    assert prompt.system_prompt(tool_calling=True) == first
    assert prompt.system_prompt(tool_calling=False) == prompt.system_prompt(tool_calling=False)
    assert prompt.system_prompt(tool_calling=True) != prompt.system_prompt(tool_calling=False)
