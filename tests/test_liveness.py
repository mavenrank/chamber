"""Run liveness is a heartbeat, not an open row.

Crashed and killed runs never call end_run, so `ended_at IS NULL` means
"never closed" — not "running". Only a recent `last_beat` counts.
"""

from __future__ import annotations

import sqlite3
import time

from chamber.trace import store as store_mod
from chamber.trace.store import open_store, run_is_live


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_migration_adds_beat_to_legacy_db(tmp_path):
    target = tmp_path / "legacy.sqlite"
    conn = _db(target)
    # Exact 0.9.0 shape: everything except last_beat.
    conn.execute(
        "CREATE TABLE run (id TEXT PRIMARY KEY, task TEXT NOT NULL, profile TEXT NOT NULL,"
        " model TEXT, started_at REAL NOT NULL, ended_at REAL, success INTEGER,"
        " summary TEXT, stopped_because TEXT,"
        " input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO run (id, task, profile, started_at) VALUES (?, ?, ?, ?)",
        ("old-1", "task", "default", time.time()),
    )
    conn.commit()
    conn.close()

    with open_store(target) as store:
        row = store.run("old-1")
        assert row is not None
        assert row["live"] is False  # silent corpse, never live

        store.start_run("new-1", "task", profile="default", model="m")
        store.beat()
        assert store.run("new-1")["live"] is True


def test_beat_and_close_transitions(tmp_path):
    target = tmp_path / "t.sqlite"
    with open_store(target) as store:
        store.start_run("r1", "task", profile="default", model="m")
        assert store.run("r1")["live"] is False  # started, not yet beating
        store.beat()
        assert store.run("r1")["live"] is True
        store.end_run(success=True, summary="done")
        assert store.run("r1")["live"] is False


def test_stale_beat_is_dead(tmp_path):
    target = tmp_path / "t.sqlite"
    with open_store(target) as store:
        store.start_run("r1", "task", profile="default", model="m")
        store.conn.execute(
            "UPDATE run SET last_beat=? WHERE id=?",
            (time.time() - store_mod.LIVE_GRACE_S - 10, "r1"),
        )
        store.conn.commit()
        assert store.run("r1")["live"] is False
        assert [r for r in store.runs() if r["live"]] == []


def test_run_is_live_pure():
    now = time.time()
    assert run_is_live({"ended_at": None, "last_beat": now}) is True
    assert run_is_live({"ended_at": 1.0, "last_beat": now}) is False
    assert run_is_live({"ended_at": None, "last_beat": None}) is False
    assert run_is_live({"ended_at": None, "last_beat": now - 10_000}) is False
