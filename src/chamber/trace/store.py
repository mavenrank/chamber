"""What happened, kept.

A run that produces an answer without a trail is a worse product than one that
shows its work, and the difference is most of the point here: "research this and
show me your sources" is not answerable without a record of every page opened, in
order, with what came out of it.

SQLite, one file, no server. Rows are appended and never deleted — a page the agent
visited and rejected is *evidence*, and pruning it would hide the most interesting
part of the trail.

The schema is deliberately flat. A run has steps, a step has actions, and a visit
records one page the agent actually looked at. The "tree" of a research session is
reconstructed by joining on step order rather than stored as nested structure,
because the interesting groupings (by domain, by query, by whether it fed the
answer) are all different views of the same rows.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chamber import paths
from chamber.urls import canonical

log = logging.getLogger(__name__)

# A run counts as live while its loop is audibly working. `ended_at IS NULL`
# alone is NOT liveness — crashed and killed runs never close their row, so
# NULL means "never closed", not "running". The loop writes `last_beat`
# every step; silence longer than this means the process is gone.
LIVE_GRACE_S = 180.0


def run_is_live(row: dict[str, Any]) -> bool:
    """True only for a run that ended open AND beat recently."""
    if row.get("ended_at") is not None:
        return False
    return (time.time() - float(row.get("last_beat") or 0)) < LIVE_GRACE_S

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id          TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    model       TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    success     INTEGER,
    summary     TEXT,
    stopped_because TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    last_beat   REAL,
    parent_run  TEXT
);

CREATE TABLE IF NOT EXISTS note (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES run(id),
    step_n    INTEGER,
    text      TEXT NOT NULL,
    at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_run ON note(run_id, id);

CREATE TABLE IF NOT EXISTS step (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL REFERENCES run(id),
    n        INTEGER NOT NULL,
    thought  TEXT,
    url      TEXT,
    title    TEXT,
    fingerprint TEXT,
    ms       INTEGER,
    at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS action (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL REFERENCES run(id),
    step_n   INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    name     TEXT NOT NULL,
    args     TEXT,
    why      TEXT,
    outcome  TEXT NOT NULL,
    message  TEXT,
    repairs  TEXT,
    ms       INTEGER,
    at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS visit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES run(id),
    step_n    INTEGER,
    url       TEXT NOT NULL,
    canonical TEXT NOT NULL,
    title     TEXT,
    content_chars INTEGER DEFAULT 0,
    controls  INTEGER DEFAULT 0,
    used      INTEGER DEFAULT 0,
    note      TEXT,
    at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_exchange (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES run(id),
    role        TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_style   TEXT,
    reasoning_effort TEXT,
    request_json TEXT,
    response_json TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    ms          INTEGER,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_step_run   ON step(run_id, n);
CREATE INDEX IF NOT EXISTS idx_action_run ON action(run_id, step_n, seq);
CREATE INDEX IF NOT EXISTS idx_visit_run  ON visit(run_id);
CREATE INDEX IF NOT EXISTS idx_visit_canon ON visit(canonical);
CREATE INDEX IF NOT EXISTS idx_model_run ON model_exchange(run_id, started_at);
"""


def canonical_url(url: str) -> str:
    """A stable identity for the same page reached by different routes.

    Without this, deduplication finds almost nothing: the same article reached from
    three places carries three different `utm_source` values and compares unequal
    every time. Shared with the reader — see `chamber/urls.py`.
    """
    return canonical(url)


@dataclass(slots=True)
class TraceStore:
    conn: sqlite3.Connection
    run_id: str = ""

    # ------------------------------------------------------------------ writes

    def start_run(
        self, run_id: str, task: str, *, profile: str, model: str = "",
        parent: str = "",
    ) -> None:
        self.run_id = run_id
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO run (id, task, profile, model, started_at, parent_run) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task, profile, model, time.time(), parent or None),
            )

    def record_note(self, n: int | None, text: str) -> None:
        """A claimed human note, with the step that read it. Thread memory."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO note (run_id, step_n, text, at) VALUES (?, ?, ?, ?)",
                (self.run_id, n, text[:4000], time.time()),
            )

    def notes(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM note WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def beat(self) -> None:
        """Mark the loop alive. Called every step; cheap single UPDATE."""
        if not self.run_id:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE run SET last_beat=? WHERE id=?", (time.time(), self.run_id)
            )

    def end_run(
        self,
        *,
        success: bool,
        summary: str,
        stopped_because: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE run SET ended_at=?, success=?, summary=?, stopped_because=?, "
                "input_tokens=?, output_tokens=? WHERE id=?",
                (
                    time.time(),
                    int(success),
                    summary,
                    stopped_because,
                    input_tokens,
                    output_tokens,
                    self.run_id,
                ),
            )

    def record_step(
        self, n: int, *, thought: str, url: str, title: str = "", fingerprint: str = "", ms: int = 0
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO step (run_id, n, thought, url, title, fingerprint, ms, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, n, thought, url, title, fingerprint, ms, time.time()),
            )

    def record_action(
        self,
        step_n: int,
        seq: int,
        *,
        name: str,
        args: dict[str, Any],
        why: str,
        outcome: str,
        message: str = "",
        repairs: list[str] | None = None,
        ms: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO action (run_id, step_n, seq, name, args, why, outcome, "
                "message, repairs, ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    step_n,
                    seq,
                    name,
                    json.dumps(args, default=str)[:4000],
                    why,
                    outcome,
                    message[:2000],
                    json.dumps(repairs or []),
                    ms,
                    time.time(),
                ),
            )

    def record_visit(
        self,
        url: str,
        *,
        step_n: int | None = None,
        title: str = "",
        content_chars: int = 0,
        controls: int = 0,
        note: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO visit (run_id, step_n, url, canonical, title, "
                "content_chars, controls, note, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    step_n,
                    url,
                    canonical_url(url),
                    title,
                    content_chars,
                    controls,
                    note,
                    time.time(),
                ),
            )

    def mark_used(self, urls: list[str]) -> None:
        """Flag the pages that actually fed the answer.

        The distinction between "looked at" and "used" is what turns a visit log
        into a source list.
        """
        canon = [canonical_url(u) for u in urls]
        with self.conn:
            self.conn.executemany(
                "UPDATE visit SET used=1 WHERE run_id=? AND canonical=?",
                [(self.run_id, c) for c in canon],
            )

    def record_model_event(self, event: str, payload: dict[str, Any]) -> None:
        """Keep the exact inspectable model boundary for a run.

        Images are represented by counts in the request payload rather than copied
        into SQLite as base64. Prompt text, tool schemas, returned text, tool calls,
        API reasoning summaries and usage are retained in full.
        """
        if not self.run_id or event not in ("llm_request", "llm_response"):
            return
        exchange_id = str(payload.get("exchange_id") or "")
        if not exchange_id:
            return
        now = time.time()
        if event == "llm_request":
            request = payload.get("request") or {}
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO model_exchange "
                    "(id, run_id, role, model, api_style, reasoning_effort, request_json, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        exchange_id,
                        self.run_id,
                        str(payload.get("role", "model")),
                        str(payload.get("model", "")),
                        str(payload.get("api_style", "")),
                        str(payload.get("reasoning_effort") or ""),
                        json.dumps(request, ensure_ascii=False, default=str),
                        now,
                    ),
                )
            return

        response = {
            "text": payload.get("text", ""),
            "reasoning_summary": payload.get("reasoning_summary", ""),
            "tool_calls": payload.get("tool_calls") or [],
            "stop_reason": payload.get("stop_reason", ""),
        }
        seconds = float(payload.get("seconds", 0) or 0)
        with self.conn:
            updated = self.conn.execute(
                "UPDATE model_exchange SET response_json=?, ended_at=?, ms=?, "
                "input_tokens=?, output_tokens=? WHERE id=? AND run_id=?",
                (
                    json.dumps(response, ensure_ascii=False, default=str),
                    now,
                    int(seconds * 1000),
                    int(payload.get("input_tokens", 0) or 0),
                    int(payload.get("output_tokens", 0) or 0),
                    exchange_id,
                    self.run_id,
                ),
            )
            if not updated.rowcount:
                self.conn.execute(
                    "INSERT INTO model_exchange "
                    "(id, run_id, role, model, response_json, started_at, ended_at, ms, input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        exchange_id,
                        self.run_id,
                        str(payload.get("role", "model")),
                        str(payload.get("model", "")),
                        json.dumps(response, ensure_ascii=False, default=str),
                        now - seconds,
                        now,
                        int(seconds * 1000),
                        int(payload.get("input_tokens", 0) or 0),
                        int(payload.get("output_tokens", 0) or 0),
                    ),
                )

    # ------------------------------------------------------------------- reads

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["live"] = run_is_live(d)
            out.append(d)
        return out

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["live"] = run_is_live(d)
        return d

    def children(self, run_id: str) -> list[dict[str, Any]]:
        """Runs continued from this one, oldest first. The thread forward."""
        try:
            rows = self.conn.execute(
                "SELECT id, task, success, ended_at, started_at FROM run"
                " WHERE parent_run=? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # pre-thread database
        return [dict(r) for r in rows]

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM step WHERE run_id=? ORDER BY n", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def actions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM action WHERE run_id=? ORDER BY step_n, seq", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def sources(self, run_id: str, *, used_only: bool = False) -> list[dict[str, Any]]:
        """Distinct pages visited, in the order first seen.

        Grouped by canonical URL with a visit count, so a page the agent returned
        to shows as one source with weight rather than three separate entries.
        """
        clause = "AND used=1" if used_only else ""
        # Ordered by rowid, not timestamp. Two visits recorded in the same tick tie
        # on `at` — the clock's resolution is coarser than the loop — and the order
        # of the trail becomes nondeterministic. The rowid is insertion order by
        # definition.
        rows = self.conn.execute(
            f"""
            SELECT canonical, MIN(id) AS seq, MIN(at) AS first_seen, COUNT(*) AS visits,
                   MAX(title) AS title, MAX(content_chars) AS content_chars,
                   MAX(used) AS used, MAX(url) AS url
            FROM visit WHERE run_id=? {clause}
            GROUP BY canonical ORDER BY seq
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def model_exchanges(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_exchange WHERE run_id=? ORDER BY started_at, rowid",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def model_transcript(self, run_id: str) -> str:
        """Full prompts and outputs at the model boundary.

        Reasoning summaries are included only when the provider returned one. They
        are summaries exposed by the API, not private chain-of-thought.
        """
        run = self.run(run_id)
        if run is None:
            return f"no such run: {run_id}"
        exchanges = self.model_exchanges(run_id)
        if not exchanges:
            return f"run {run_id}\n  no model exchanges were recorded"
        lines = [f"run {run_id} — model transcript", f"task: {run['task']}"]
        for index, row in enumerate(exchanges, 1):
            lines.extend(
                [
                    "",
                    f"=== {index}. {row['role']} · {row['model']} · {row['ms'] or 0}ms ===",
                    f"transport: {row['api_style'] or 'unknown'} · reasoning: {row['reasoning_effort'] or 'unspecified'}",
                    f"tokens: {row['input_tokens'] or 0} in / {row['output_tokens'] or 0} out",
                ]
            )
            request = json.loads(row["request_json"] or "{}")
            if request.get("system"):
                lines.extend(["", "[system]", request["system"]])
            for message in request.get("messages") or []:
                suffix = f" · {message.get('images')} image(s)" if message.get("images") else ""
                lines.extend(["", f"[{message.get('role', 'message')}{suffix}]", message.get("content", "")])
            tools = request.get("tools") or []
            if tools:
                lines.extend(["", "[available tools]", json.dumps(tools, ensure_ascii=False, indent=2)])
            response = json.loads(row["response_json"] or "{}")
            if response.get("reasoning_summary"):
                lines.extend(["", "[provider reasoning summary]", response["reasoning_summary"]])
            if response.get("text"):
                lines.extend(["", "[model output]", response["text"]])
            if response.get("tool_calls"):
                lines.extend(["", "[tool calls]", json.dumps(response["tool_calls"], ensure_ascii=False, indent=2)])
            if not response:
                lines.extend(["", "[response missing — the request did not finish]"])
        return "\n".join(lines)

    def timeline(self, run_id: str) -> str:
        """The run as an indented tree — steps, their actions, and the pages seen."""
        run = self.run(run_id)
        if run is None:
            return f"no such run: {run_id}"

        lines = [
            f"run {run_id}",
            f"  task: {run['task']}",
            f"  model: {run['model'] or '(none)'} · profile: {run['profile']}",
        ]

        by_step: dict[int, list[dict[str, Any]]] = {}
        for action in self.actions(run_id):
            by_step.setdefault(action["step_n"], []).append(action)

        # A step row is written *after* its actions, so a run that was interrupted
        # mid-step leaves actions with no step. Those are the most interesting
        # actions in the trace — they are what the run died doing — so the union of
        # both tables is walked rather than just the step table.
        steps_by_n = {s["n"]: s for s in self.steps(run_id)}
        for n in sorted(set(steps_by_n) | set(by_step)):
            step = steps_by_n.get(n)
            if step is None:
                lines.append(f"  • step {n}  [incomplete — the run stopped here]")
            else:
                lines.append(f"  • step {n}  {step['url'][:70]}")
                if step["thought"]:
                    lines.append(f"      “{step['thought'][:110]}”")
            for action in by_step.get(n, []):
                ok = "✓" if action["outcome"] == "ok" else "✗"
                detail = (action["message"] or "")[:80]
                lines.append(f"      {ok} {action['name']:<14} {detail}")
                for repair in json.loads(action["repairs"] or "[]"):
                    lines.append(f"          ↳ chamber adjusted: {repair}")

        sources = self.sources(run_id)
        if sources:
            lines.append("")
            lines.append(f"  sources ({len(sources)} distinct pages)")
            for source in sources:
                flag = "★" if source["used"] else " "
                visits = f" ×{source['visits']}" if source["visits"] > 1 else ""
                lines.append(
                    f"    {flag} {source['canonical'][:80]}{visits}"
                    + (f"  — {source['title'][:40]}" if source["title"] else "")
                )

        if run["summary"]:
            lines.append("")
            lines.append(f"  result: {'✓' if run['success'] else '✗'} {run['summary'][:400]}")
        exchanges = self.model_exchanges(run_id)
        if exchanges:
            lines.append(
                f"  model exchanges: {len(exchanges)} (use chamber trace {run_id} --llm for full prompts and outputs)"
            )
        return "\n".join(lines)


@contextlib.contextmanager
def open_store(path: Path | None = None) -> Iterator[TraceStore]:
    """Open (and migrate) the trace database."""
    target = path or paths.db_path()
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # WAL so a reader (a status view, another shell) can look at a live run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(run)").fetchall()]
    if "last_beat" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN last_beat REAL")
    if "parent_run" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN parent_run TEXT")
    try:
        yield TraceStore(conn)
    finally:
        conn.close()
