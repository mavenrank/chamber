"""Chamber Console server — localhost, read-only (Phase 1).

Desk × Console contract: Console owns URLs (`/` → console); Desk stays
server-free and is NOT served here. History flows only through
`display/queries.py`. See CHANGELOG 0.11.0.

Serves the built Console (`dist/console.html`) plus `/api/query` backed by
`display/queries.py`. Stdlib only, no new dependencies. Nothing is written,
no browser is launched, the loop is never contacted — SQLite WAL makes
reading an in-flight run safe.

Run it with:  `chamber console [--port 5192]`
"""

from __future__ import annotations

import json
import logging
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from chamber.display import queries as _q

log = logging.getLogger(__name__)

DIST = Path(__file__).with_name("dist")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5192


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
                return self._json({"ok": False, "error": f"unknown op: {op}"}, 400)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:300]}, 500)
        if parsed.path in ("/", "/index.html"):
            self.path = "/console.html"
        return super().do_GET()


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Block serving forever. Ctrl+C stops it (handled by the CLI)."""
    with ThreadingHTTPServer((host, port), ConsoleHandler) as httpd:
        httpd.serve_forever()
