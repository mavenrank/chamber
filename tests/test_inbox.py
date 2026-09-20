"""Phase 2 file mailbox: notes in, pause flag, traversal rejected."""

from __future__ import annotations

import pytest

from chamber import inbox


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    return tmp_path


def test_note_round_trip(home):
    note = inbox.post_note("20260920-120000-abc123", "scroll slower")
    assert note["id"]
    pending = inbox.pending_notes("20260920-120000-abc123")
    assert [n["id"] for n in pending] == [note["id"]]
    claimed = inbox.claim_notes("20260920-120000-abc123")
    assert [n["text"] for n in claimed] == ["scroll slower"]
    assert inbox.pending_notes("20260920-120000-abc123") == []
    assert inbox.claim_notes("20260920-120000-abc123") == []


def test_empty_note_rejected(home):
    with pytest.raises(ValueError):
        inbox.post_note("20260920-120000-abc123", "   ")


def test_bad_run_id_rejected(home):
    for bad in ("../x", "..", "", "a/b", "x" * 80):
        with pytest.raises(ValueError):
            inbox.post_note(bad, "hi")
    assert inbox.pending_notes("../x") == []
    assert inbox.claim_notes("../x") == []
    assert inbox.is_paused("../x") is False


def test_pause_round_trip(home):
    run = "20260920-120000-abc123"
    assert inbox.is_paused(run) is False
    inbox.set_paused(run, True)
    assert inbox.is_paused(run) is True
    inbox.set_paused(run, False)
    assert inbox.is_paused(run) is False
