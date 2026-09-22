"""Phase 2 server: POST inbox/pause, run_state, SSE tail."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from chamber.display.server import ConsoleHandler, _parse_cursor


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    return tmp_path


@pytest.fixture()
def server(home):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


def _request_once(base, path, payload):
    import urllib.error

    if payload is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def _request(base, path, payload):
    """One retry: Windows loopback sockets RST under parallel-suite load."""
    try:
        return _request_once(base, path, payload)
    except ConnectionError:
        return _request_once(base, path, payload)


def _post(base, path, payload):
    return _request(base, path, payload)


def _get(base, path):
    return _request(base, path, None)


def test_inbox_and_pause_round_trip(server):
    run = "20260920-120000-abc123"
    status, body = _post(server, "/api/inbox", {"run_id": run, "text": "go slower"})
    assert status == 200 and body["ok"] is True
    assert body["note"]["text"] == "go slower"

    status, body = _post(server, "/api/pause", {"run_id": run, "paused": True})
    assert status == 200 and body["state"]["paused"] is True

    status, body = _get(server, f"/api/query?op=run_state&run_id={run}")
    assert status == 200 and body["ok"] is True
    assert body["paused"] is True
    assert [n["text"] for n in body["pending_notes"]] == ["go slower"]

    status, body = _post(server, "/api/inbox", {"run_id": "../evil", "text": "x"})
    assert status == 400
    status, body = _post(server, "/api/inbox", {"run_id": run, "text": "   "})
    assert status == 400


def test_supervised_run_endpoints(server, monkeypatch):
    import chamber.supervisor as sup

    seen = {}

    def fake_start(spec, parent="", notes=None):
        seen["spec"] = spec
        seen["parent"] = parent
        seen["notes"] = notes
        return {"run_id": "20260920-120000-abc123", "managed": True, "pid": 1}

    def fake_stop(run_id, grace_s=10.0):
        seen["stopped"] = run_id
        return {"run_id": run_id, "managed": True, "result": "stopped"}

    monkeypatch.setattr(sup, "start_run", fake_start)
    monkeypatch.setattr(sup, "stop_run", fake_stop)
    monkeypatch.setattr(sup, "managed_runs", lambda: [{"run_id": "x", "managed": True}])

    status, body = _post(server, "/api/runs", {"task": "  ", "profile": "default"})
    assert status == 400
    status, body = _post(server, "/api/runs", {"task": "do it", "profile": "../evil"})
    assert status == 400
    status, body = _post(server, "/api/runs", {"task": "do it", "profile": "shop", "max_steps": 5})
    assert status == 202 and body["run_id"] == "20260920-120000-abc123"
    assert seen["spec"].profile == "shop"

    status, body = _post(server, "/api/runs/20260920-120000-abc123/stop", {})
    assert status == 200 and body["result"] == "stopped"
    assert seen["stopped"] == "20260920-120000-abc123"

    # Dot-segments never survive URL normalization — 404 before any check.
    status, body = _post(server, "/api/runs/../evil/stop", {})
    assert status == 404
    status, body = _post(server, "/api/runs/%2E%2E%2Fevil/stop", {})
    assert status == 400

    status, body = _get(server, "/api/query?op=managed_runs")
    assert status == 200 and body["runs"] == [{"run_id": "x", "managed": True}]

    status, body = _get(server, "/api/runs/20260920-120000-abc123/log?offset=0")
    assert status == 200 and body["text"] == ""


def test_cursor_parsing_is_lenient():
    assert _parse_cursor("3:9:1:0") == (3, 9, 1, 0)
    assert _parse_cursor("") == (0, 0, 0, 0)
    assert _parse_cursor("garbage") == (0, 0, 0, 0)
    assert _parse_cursor("5") == (5, 0, 0, 0)


def test_tail_since_picks_up_new_rows(home):
    from chamber.display.server import _tail_since
    from chamber.trace.store import open_store

    run = "20260920-120000-abc123"
    with open_store() as store:
        store.start_run(run, "task", profile="default", model="m")
        store.record_step(1, thought="hi", url="https://example.com")
        batch, cursor = _tail_since(store.conn, run, (0, 0, 0, 0))
        assert [r["kind"] for r in batch["rows"]] == ["step"]
        assert cursor[0] > 0
        batch2, _ = _tail_since(store.conn, run, cursor)
        assert batch2["rows"] == []
        assert batch2["paused"] is False


def test_live_stream_opens_and_ticks(server):
    req = urllib.request.Request(f"{server}/api/live?run_id=20260920-120000-abc123")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        assert resp.headers.get_content_type() == "text/event-stream"
        head = resp.read(64).decode("utf-8", "replace")
        assert head.startswith(": connected")
