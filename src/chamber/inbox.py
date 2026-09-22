"""Human → loop mailbox, as files (Phase 2).

The Console server and the agent loop run in *different* processes, so an
in-memory queue cannot connect them. Instead each run owns a mailbox under
its run directory:

    runs/<run_id>/inbox/new/<uuid>.json    notes waiting for the loop
    runs/<run_id>/inbox/done/<uuid>.json   notes the loop has consumed
    runs/<run_id>/control.json             {"paused": bool, ...}

One note per file: creation and `rename` are atomic on local disks, so the
server can append while the loop drains without a lock and neither loses a
note. The loop checks the mailbox at the top of every step (beside the
`controlled` check); the Console writes through `POST /api/inbox`.

P2 scope, stated plainly: notes are transient coordination. Consumed notes
leave the mailbox — they survive in the model's context and the Desk feed,
not in the trace store. Persistence is Phase 3 work.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from chamber import paths

log = logging.getLogger(__name__)

_SEQ = itertools.count()

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,64}")


def check_run_id(run_id: str) -> str:
    """Reject path traversal before a run id ever touches the filesystem."""
    if not _RUN_ID.fullmatch(run_id or ""):
        raise ValueError(f"bad run id: {run_id!r}")
    return run_id


def inbox_dir(run_id: str) -> Path:
    d = paths.run_dir(check_run_id(run_id)) / "inbox"
    (d / "new").mkdir(parents=True, exist_ok=True)
    (d / "done").mkdir(parents=True, exist_ok=True)
    return d


def control_path(run_id: str) -> Path:
    return paths.run_dir(check_run_id(run_id)) / "control.json"


def post_note(run_id: str, text: str) -> dict[str, Any]:
    """File one note for the loop. Returns its envelope (with id)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty note")
    note = {
        "id": uuid.uuid4().hex[:12],
        "text": text[:4000],
        "at": time.time(),
    }
    # Sortable names keep claim order chronological: context before
    # steering, older notes before newer ones._ns alone can collide on
    # coarse clocks, so pid+sequence disambiguate.
    stamp = f"{time.time_ns():020d}-{os.getpid():06d}-{next(_SEQ):06d}"
    path = inbox_dir(run_id) / "new" / f"{stamp}-{note['id']}.json"
    path.write_text(json.dumps(note), encoding="utf-8")
    return note


def pending_notes(run_id: str) -> list[dict[str, Any]]:
    """Notes waiting for the loop, oldest first. Never raises."""
    try:
        files = sorted((inbox_dir(run_id) / "new").glob("*.json"))
    except (OSError, ValueError):
        return []
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def claim_notes(run_id: str) -> list[dict[str, Any]]:
    """Atomically move waiting notes to done/ and return them, oldest first."""
    try:
        box = inbox_dir(run_id)
    except ValueError:
        return []
    claimed = []
    for f in sorted((box / "new").glob("*.json")):
        try:
            note = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            f.rename(box / "done" / f.name)
        except OSError:
            continue  # another drainer took it; whichever owns it, it is kept
        claimed.append(note)
    return claimed


def is_paused(run_id: str) -> bool:
    """Whether a human currently holds the loop. Never raises."""
    return bool(_read_control(run_id).get("paused", False))


def _read_control(run_id: str) -> dict[str, Any]:
    try:
        data = json.loads(control_path(run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_paused(run_id: str, paused: bool) -> dict[str, Any]:
    """Hold or release the loop. The loop polls this every step."""
    state = {**_read_control(run_id), "paused": bool(paused), "at": time.time()}
    control_path(run_id).write_text(json.dumps(state), encoding="utf-8")
    return state


def request_stop(run_id: str) -> dict[str, Any]:
    """Ask the loop to finish cleanly at the next step boundary.

    Distinct from pause: pause waits, stop exits — the trace closes with
    "stopped from Chamber Console" instead of hanging open.
    """
    state = {**_read_control(run_id), "stop": True, "at": time.time()}
    control_path(run_id).write_text(json.dumps(state), encoding="utf-8")
    return state


def is_stop_requested(run_id: str) -> bool:
    """Whether a stop was requested. Never raises."""
    try:
        return bool(_read_control(run_id).get("stop", False))
    except ValueError:
        return False
