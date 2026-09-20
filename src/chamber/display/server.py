"""Chamber Console server — localhost (Phase 2: live tail + mailbox writes).

Desk × Console contract: Console owns URLs (`/` → console); Desk stays
server-free and is NOT served here. History flows only through
`display/queries.py`. See CHANGELOG 0.11.0 / 0.12.0.

Read-mostly, stdlib only. Reads: static bundle, `/api/query`, and the
`/api/live` event stream, which tails the WAL-mode trace store — safe to
read an in-flight run. Writes (the deliberate Phase 2 half): `POST
/api/inbox` files a human note and `POST /api/pause` holds/releases the
loop, both as run-dir files the loop polls. No browser is launched and no
process is signalled.

Run it with:  `chamber console [--port 5192]`
"""

from __future__ import annotations

import json
import logging
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from chamber import inbox as _box
from chamber.display import queries as _q

log = logging.getLogger(__name__)

DIST = Path(__file__).with_name("dist")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5192

# How long one `/api/live` stream is held open before the client reconnects.
_LIVE_HOLD_S = 20.0
_LIVE_POLL_S = 1.0


class ConsoleHandler(SimpleHTTPRequestHandler):
    """Static `dist/` + JSON query API. Localhost only by construction."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, directory=str(DIST), **kwargs)

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # the terminal stays the transcript; use --verbose for these

    # Browsers routinely abandon in-flight polls (StrictMode double-fetch,
    # auto-refresh overlap, navigating away). A dead socket is not an error
    # worth a traceback — swallow it and move on.
    _DEAD_SOCKET = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

    def _json(self, payload: object, status: int = 200) -> None:
        try:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self._DEAD_SOCKET:
            log.debug("desk query client went away mid-response")

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except self._DEAD_SOCKET:
            log.debug("desk query client went away mid-request")

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/query":
            args = parse_qs(parsed.query)
            op = (args.get("op") or [""])[0]
            try:
                if op == "list_runs":
                    try:
                        limit = int((args.get("limit") or ["20"])[0] or 20)
                    except (TypeError, ValueError):
                        limit = 20
                    return self._json(
                        {"ok": True, "runs": _q.list_runs(limit=min(max(limit, 1), 50))}
                    )
                if op == "get_run":
                    return self._json(
                        {"ok": True, **_q.get_run((args.get("run_id") or [""])[0])}
                    )
                if op == "list_profiles":
                    return self._json({"ok": True, "profiles": _q.list_profiles()})
                if op == "get_environment":
                    return self._json({"ok": True, **_q.get_environment()})
                if op == "run_state":
                    return self._json({"ok": True, **_q.run_state((args.get("run_id") or [""])[0])})
                return self._json({"ok": False, "error": f"unknown op: {op}"}, 400)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:300]}, 500)
        if parsed.path == "/api/live":
            return self._serve_live(parse_qs(urlparse(self.path).query))
        if parsed.path in ("/", "/index.html"):
            self.path = "/console.html"
        return super().do_GET()

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except self._DEAD_SOCKET:
            log.debug("desk query client went away mid-post")

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/inbox", "/api/pause"):
            return self._json({"ok": False, "error": "unknown endpoint"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        try:
            body = json.loads(self.rfile.read(max(length, 0)) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "body must be JSON"}, 400)
        try:
            run_id = _box.check_run_id(str(body.get("run_id", "")))
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        try:
            if parsed.path == "/api/inbox":
                return self._json({"ok": True, "note": _box.post_note(run_id, str(body.get("text", "")))})
            return self._json({"ok": True, "state": _box.set_paused(run_id, bool(body.get("paused")))})
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except OSError as exc:
            return self._json({"ok": False, "error": str(exc)[:200]}, 500)

    def _serve_live(self, args: dict[str, list[str]]) -> None:
        """Hold an SSE stream tailing one run's trace rows. EventSource
        reconnects when the hold expires; the cursor params make it lossless."""
        try:
            run_id = _box.check_run_id((args.get("run_id") or [""])[0])
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        cursor = _parse_cursor((args.get("cursor") or ["0:0:0:0"])[0])
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except self._DEAD_SOCKET:
            return
        deadline = time.monotonic() + _LIVE_HOLD_S
        seq = 0
        while time.monotonic() < deadline:
            try:
                batch, cursor = _tail_since(run_id, cursor)
                if batch["rows"]:
                    seq += 1
                    line = f"id: {seq}\nevent: tick\ndata: {json.dumps(batch, default=str)}\n\n"
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                else:
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()
                time.sleep(_LIVE_POLL_S)
            except self._DEAD_SOCKET:
                return
            except Exception as exc:  # a bad poll must not kill the stream
                log.debug("live tail poll failed: %s", exc)
                time.sleep(_LIVE_POLL_S)


def _slim_exchange(row: object) -> dict[str, object]:
    """Exchange row minus the full prompt/response bodies.

    The stream carries who/what/when; full text stays behind `get_run`
    (the Session detail tab fetches it on demand).
    """
    d = dict(row)  # type: ignore[arg-type]
    response = {}
    try:
        response = json.loads(d.get("response_json") or "{}")
    except (TypeError, ValueError):
        response = {}
    calls = response.get("tool_calls") or []
    return {
        "kind": "exchange",
        "rowid": d.get("rowid"),
        "role": d.get("role"),
        "model": d.get("model"),
        "ms": d.get("ms"),
        "input_tokens": d.get("input_tokens"),
        "output_tokens": d.get("output_tokens"),
        "text": str(response.get("text", ""))[:300],
        "tool_calls": [
            {"name": c.get("name", "")} for c in calls if isinstance(c, dict)
        ][:4],
    }


def _parse_cursor(raw: str) -> tuple[int, int, int, int]:
    """`step:action:exchange:visit` row watermarks. Lenient by design."""
    try:
        parts = [int(p or 0) for p in (raw or "").split(":")[:4]]
    except (TypeError, ValueError):
        return (0, 0, 0, 0)
    parts += [0] * (4 - len(parts))
    return (parts[0], parts[1], parts[2], parts[3])


def _tail_since(
    run_id: str, cursor: tuple[int, int, int, int]
) -> tuple[dict[str, object], tuple[int, int, int, int]]:
    """Rows landed since the watermarks, plus the new watermarks."""
    from chamber.trace.store import open_store

    step_id, action_id, x_rowid, visit_id = cursor
    with open_store() as store:
        conn = store.conn
        steps = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM step WHERE run_id=? AND id>? ORDER BY id", (run_id, step_id)
            ).fetchall()
        ]
        actions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM action WHERE run_id=? AND id>? ORDER BY id", (run_id, action_id)
            ).fetchall()
        ]
        exchanges = [
            _slim_exchange(r)
            for r in conn.execute(
                "SELECT rowid, * FROM model_exchange WHERE run_id=? AND rowid>? "
                "ORDER BY rowid",
                (run_id, x_rowid),
            ).fetchall()
        ]
        visits = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM visit WHERE run_id=? AND id>? ORDER BY id", (run_id, visit_id)
            ).fetchall()
        ]
    for s in steps:
        s["kind"] = "step"
    for a in actions:
        a["kind"] = "action"
    for v in visits:
        v["kind"] = "visit"
    rows = steps + actions + exchanges + visits
    new_cursor = (
        max([step_id] + [r["id"] for r in steps]),
        max([action_id] + [r["id"] for r in actions]),
        max([x_rowid] + [r["rowid"] for r in exchanges]),
        max([visit_id] + [r["id"] for r in visits]),
    )
    return (
        {
            "rows": rows,
            "paused": _box.is_paused(run_id),
            "pending_notes": _box.pending_notes(run_id),
        },
        new_cursor,
    )


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Block serving forever. Ctrl+C stops it (handled by the CLI)."""
    with ThreadingHTTPServer((host, port), ConsoleHandler) as httpd:
        httpd.serve_forever()
