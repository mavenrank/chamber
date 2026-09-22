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

# Ended runs are immutable: full payloads cached per process, forever.
# Live runs bypass. Keyed (op, run_id); ~1MB worst case per entry.
_ENDED_CACHE: dict[tuple[str, str], dict[str, object]] = {}


def _cached_full(op: str, run_id: str, loader):  # loader() -> dict
    """Serve ended runs from memory; live runs always fresh.

    A 1ms `ended_at` lookup saves re-shipping ~765KB per poll for the big
    runs reviewers actually open. Correctness rests on one fact: a row with
    `ended_at` set never changes again.
    """
    hit = _ENDED_CACHE.get((op, run_id))
    if hit is not None:
        return dict(hit)
    payload = loader()
    run = payload.get("run") if isinstance(payload, dict) else None
    if isinstance(run, dict) and run.get("ended_at") is not None:
        _ENDED_CACHE[(op, run_id)] = payload
    return payload


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
                    run_id = (args.get("run_id") or [""])[0]
                    return self._json(
                        {"ok": True, **_cached_full("get_run", run_id, lambda: _q.get_run(run_id))}
                    )
                if op == "thought_loop":
                    run_id = (args.get("run_id") or [""])[0]
                    try:
                        since = int((args.get("since_step") or ["0"])[0] or 0)
                    except (TypeError, ValueError):
                        since = 0
                    if since <= 0:
                        payload = _cached_full(
                            "thought_loop", run_id, lambda: _q.thought_loop(run_id)
                        )
                    else:
                        payload = _q.thought_loop(run_id, since_step=since)
                    return self._json({"ok": True, **payload})
                if op == "exchange_detail":
                    return self._json(
                        {
                            "ok": True,
                            **_q.exchange_detail(
                                (args.get("run_id") or [""])[0],
                                (args.get("exchange_id") or [""])[0],
                            ),
                        }
                    )
                if op == "list_profiles":
                    return self._json({"ok": True, "profiles": _q.list_profiles()})
                if op == "get_environment":
                    return self._json({"ok": True, **_q.get_environment()})
                if op == "run_state":
                    return self._json({"ok": True, **_q.run_state((args.get("run_id") or [""])[0])})
                if op == "managed_runs":
                    from chamber import supervisor as _sup

                    return self._json({"ok": True, "runs": _sup.managed_runs()})
                if op == "restart_prefill":
                    return self._json(
                        {"ok": True, **_q.restart_context((args.get("run_id") or [""])[0])}
                    )
                if op == "thread":
                    return self._json(
                        {"ok": True, **_q.thread((args.get("run_id") or [""])[0])}
                    )
                if op == "usage":
                    return self._json({"ok": True, **_q.usage_by_model()})
                if op == "exchange_detail":
                    return self._json(
                        {
                            "ok": True,
                            **_q.exchange_detail(
                                (args.get("run_id") or [""])[0],
                                (args.get("exchange_id") or [""])[0],
                            ),
                        }
                    )
                return self._json({"ok": False, "error": f"unknown op: {op}"}, 400)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:300]}, 500)
        if parsed.path == "/api/live":
            return self._serve_live(parse_qs(urlparse(self.path).query))
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "log":
            from chamber import supervisor as _sup

            try:
                run_id = _box.check_run_id(parts[2])
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            args = parse_qs(parsed.query)
            try:
                offset = int((args.get("offset") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                offset = 0
            return self._json({"ok": True, **_sup.read_log(run_id, offset)})
        if parsed.path in ("/", "/index.html"):
            self.path = "/console.html"
        return super().do_GET()

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except self._DEAD_SOCKET:
            log.debug("desk query client went away mid-post")

    def _read_json_body(self) -> tuple[dict[str, object] | None, str]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 65536:
            return None, "body too large"
        try:
            raw = self.rfile.read(max(length, 0)) or b"{}"
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, "body must be JSON"
        if not isinstance(data, dict):
            return None, "body must be a JSON object"
        return data, ""

    def _handle_post(self) -> None:
        from chamber import supervisor as _sup

        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/api/inbox", "/api/pause"):
            body, err = self._read_json_body()
            if body is None:
                return self._json({"ok": False, "error": err}, 400)
            try:
                run_id = _box.check_run_id(str(body.get("run_id", "")))
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            try:
                if path == "/api/inbox":
                    return self._json(
                        {"ok": True, "note": _box.post_note(run_id, str(body.get("text", "")))}
                    )
                return self._json(
                    {"ok": True, "state": _box.set_paused(run_id, bool(body.get("paused")))}
                )
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"ok": False, "error": str(exc)[:200]}, 500)
        if path == "/api/runs":
            # Start a supervised run. Inheriting the server's environment is
            # deliberate: same `.env`, same models — the Start form shows the
            # resolved config first so the click is informed consent.
            body, err = self._read_json_body()
            if body is None:
                return self._json({"ok": False, "error": err}, 400)
            from_run = str(body.get("from_run_id", "") or "")
            what_next = str(body.get("what_next", "") or "")
            parent, notes = "", []
            task = str(body.get("task", ""))
            if from_run:
                # Restart-as-new: context first, steering second. The task
                # defaults to the old one; the thread link is the parent id.
                ctx = _q.restart_context(from_run)
                if ctx.get("run") is None:
                    return self._json(
                        {"ok": False, "error": ctx.get("error", "no such run")}, 400
                    )
                parent = from_run
                notes.append(ctx["context_note"])
                if not task.strip():
                    task = ctx["task"]
            if what_next.strip():
                notes.append(what_next.strip())
            try:
                spec = _sup.RunSpec(
                    task=task,
                    profile=str(body.get("profile", "") or "default"),
                    start_url=str(body.get("start_url", "") or ""),
                    max_steps=body.get("max_steps", 40),
                    model=str(body.get("model", "") or ""),
                )
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            try:
                return self._json(
                    {"ok": True, **_sup.start_run(spec, parent=parent, notes=notes)}, 202
                )
            except RuntimeError as exc:
                return self._json({"ok": False, "error": str(exc)[:300]}, 500)
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "stop":
            try:
                run_id = _box.check_run_id(parts[2])
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            try:
                return self._json({"ok": True, **_sup.stop_run(run_id)})
            except OSError as exc:
                return self._json({"ok": False, "error": str(exc)[:200]}, 500)
        return self._json({"ok": False, "error": "unknown endpoint"}, 404)

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
        from chamber.trace.store import open_store

        deadline = time.monotonic() + _LIVE_HOLD_S
        seq = 0
        # One read connection for the whole hold — not open-per-second.
        # Migration runs once here; WAL lets the writer proceed regardless.
        with open_store() as store:
            conn = store.conn
            while time.monotonic() < deadline:
                try:
                    batch, cursor = _tail_since(conn, run_id, cursor)
                    if batch["rows"]:
                        seq += 1
                        line = (
                            f"id: {seq}\nevent: tick\n"
                            f"data: {json.dumps(batch, default=str)}\n\n"
                        )
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
    question = ""
    try:
        request = json.loads(d.get("request_json") or "{}")
        messages = request.get("messages") or []
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str) and content.strip():
                    question = content[:300]
                    break
    except (TypeError, ValueError):
        question = ""
    return {
        "kind": "exchange",
        "id": d.get("id"),
        "started_at": d.get("started_at"),
        "rowid": d.get("rowid"),
        "role": d.get("role"),
        "model": d.get("model"),
        "ms": d.get("ms"),
        "input_tokens": d.get("input_tokens"),
        "output_tokens": d.get("output_tokens"),
        "text": str(response.get("text", ""))[:300],
        "question": question,
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
    conn, run_id: str, cursor: tuple[int, int, int, int]
) -> tuple[dict[str, object], tuple[int, int, int, int]]:
    """Rows landed since the watermarks, plus the new watermarks.

    Takes an open read connection — the stream holds one for its whole
    duration instead of opening per poll.
    """
    step_id, action_id, x_rowid, visit_id = cursor
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


# NOTE (standing decision): there is intentionally NO /desk route. The Desk
# bundle is loop-owned and fed over a private channel; serving it over HTTP
# would imply a snapshot transport that doesn't exist. Trace replay lives in
# the Session Loop tab (`thought_loop`), which is the same data honestly
# labeled. Revisit only alongside a real snapshot bus (see contract §3).
