"""Supervised runs — start/stop agent runs from the Console (Phase 3a).

Until now runs were born in terminals: a human typed `chamber run` and the
server could only read the trace afterward. The supervisor flips that: the
Console POSTs a task, the server spawns the run as a child process, tracks
it, tails its log, and stops it — gracefully first (a stop flag the loop
honours at the next step boundary), forcibly only if it won't die.

Deliberate limits, stated plainly:

* **Localhost only**, like the rest of the server. A button that spends API
  money must never face a network.
* **One project root.** The child runs `uv run --no-sync chamber run` with
  the repo root as its working directory, so it reads the same `.env` the
  terminal runs use. Root is derived from the installed `chamber` package,
  overridable with `CHAMBER_PROJECT_ROOT`.
* **Logs, not pipes.** The child's stdout/stderr goes to
  `runs/<run_id>/console.log`. No pipe backpressure can wedge either side,
  and a dead server never takes a run down with it.
* **Registry is best-effort.** Live handles are kept in memory; anything the
  DB says is still running (`ended_at IS NULL`) but isn't in the registry
  is reported as unmanaged. Restarting the server never orphans silently —
  orphans are listed, not hidden.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chamber
from chamber import inbox, paths

log = logging.getLogger(__name__)

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}")

# Live child handles in this server process. Keyed by run id.
_REGISTRY: dict[str, subprocess.Popen] = {}


def project_root() -> Path:
    """Repo root the child runs in. Override with CHAMBER_PROJECT_ROOT."""
    raw = os.environ.get("CHAMBER_PROJECT_ROOT")
    if raw:
        return Path(raw).expanduser()
    # .../chamber/src/chamber/__init__.py -> .../chamber
    return Path(chamber.__file__).resolve().parents[2]


def new_run_id() -> str:
    """Same shape the loop mints itself: timestamp plus randomness."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def check_profile(name: str) -> str:
    if not _PROFILE.fullmatch(name or ""):
        raise ValueError(f"bad profile: {name!r}")
    return name


@dataclass(slots=True)
class RunSpec:
    """Validated request to start a run."""

    task: str
    profile: str = "default"
    start_url: str = ""
    max_steps: int = 40
    model: str = ""

    def __post_init__(self) -> None:
        self.task = (self.task or "").strip()
        if not self.task:
            raise ValueError("task is required")
        if len(self.task) > 4000:
            raise ValueError("task is too long (4000 chars)")
        self.profile = check_profile(self.profile)
        self.start_url = (self.start_url or "").strip()
        if self.start_url and not re.match(r"https?://", self.start_url):
            raise ValueError("start_url must be http(s)")
        try:
            self.max_steps = int(self.max_steps)
        except (TypeError, ValueError):
            raise ValueError("max_steps must be a number") from None
        if not 1 <= self.max_steps <= 200:
            raise ValueError("max_steps must be 1..200")
        self.model = (self.model or "").strip()


def build_command(spec: RunSpec, run_id: str, root: Path) -> tuple[list[str], Path]:
    """Child argv + working directory. `uv` owns deps and venv (`uv sync`
    once; `--no-sync` at spawn so a headless launch never blocks resolving)."""
    argv = [
        "uv",
        "run",
        "--no-sync",
        "chamber",
        "run",
        spec.task,
        "--profile",
        spec.profile,
        "--max-steps",
        str(spec.max_steps),
        "--run-id",
        run_id,
    ]
    if spec.start_url:
        argv += ["--url", spec.start_url]
    if spec.model:
        argv += ["--model", spec.model]
    return argv, root


def start_run(
    spec: RunSpec,
    *,
    parent: str = "",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Spawn a run. Returns its handle (JSON-safe).

    `parent` links the thread (recorded by the child via CHAMBER_PARENT_RUN);
    `notes` are filed into the mailbox in order *before* spawn, so context
    lands first and the user's steering second — both read at step 1.
    """
    run_id = new_run_id()
    if parent:
        inbox.check_run_id(parent)
    for text in notes or []:
        if str(text or "").strip():
            inbox.post_note(run_id, str(text))
    root = project_root()
    if shutil.which("uv") is None:
        raise RuntimeError("`uv` is not on PATH — install it or start runs from a terminal")
    argv, cwd = build_command(spec, run_id, root)
    env = dict(os.environ)
    if parent:
        env["CHAMBER_PARENT_RUN"] = parent
    log_path = paths.run_dir(run_id) / "console.log"
    with open(log_path, "ab") as log_file:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(f"could not spawn run: {exc}") from exc
    # The child holds its own copy of the fd past this point.
    _REGISTRY[run_id] = proc
    log.info("supervised run %s (pid %s): %s", run_id, proc.pid, spec.task[:80])
    return {
        "run_id": run_id,
        "task": spec.task,
        "profile": spec.profile,
        "pid": proc.pid,
        "started_at": time.time(),
        "managed": True,
        "parent": parent,
    }


def _reap(run_id: str) -> str:
    """Drop finished children from the registry. Returns 'running'/'exited'."""
    proc = _REGISTRY.get(run_id)
    if proc is None:
        return "unknown"
    if proc.poll() is None:
        return "running"
    del _REGISTRY[run_id]
    return "exited"


def managed_runs() -> list[dict[str, Any]]:
    """Live runs: managed handles first, then unmanaged orphans from the DB."""
    from chamber.trace.store import open_store

    out = []
    for run_id in list(_REGISTRY):
        state = _reap(run_id)
        if state == "running":
            proc = _REGISTRY[run_id]
            out.append({"run_id": run_id, "pid": proc.pid, "managed": True})
    try:
        with open_store() as store:
            rows = store.runs(limit=50)
    except Exception:
        rows = []
    known = {r["run_id"] for r in out}
    for r in rows:
        # Liveness is the heartbeat flag, not the open row: crashed runs
        # never close and must not masquerade as orphans forever.
        if r.get("live") and r["id"] not in known:
            out.append(
                {
                    "run_id": r["id"],
                    "task": r.get("task", ""),
                    "profile": r.get("profile", ""),
                    "managed": False,
                }
            )
    return out


def stop_run(run_id: str, grace_s: float = 10.0) -> dict[str, Any]:
    """Stop a run: graceful flag first, terminate only if it won't die.

    The flag lands in `control.json`, which the loop honours at the next
    step boundary — the trace closes cleanly with "stopped from Console".
    """
    inbox.check_run_id(run_id)
    inbox.request_stop(run_id)
    proc = _REGISTRY.get(run_id)
    if proc is None:
        return {"run_id": run_id, "managed": False, "result": "flagged"}
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        log.warning("run %s ignored stop flag; terminating", run_id)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _reap(run_id)
    return {"run_id": run_id, "managed": True, "result": "stopped"}


def read_log(run_id: str, offset: int = 0, limit: int = 65536) -> dict[str, Any]:
    """Tail the run's console log. Returns text plus the new offset."""
    inbox.check_run_id(run_id)
    path = paths.run_dir(run_id) / "console.log"
    try:
        size = path.stat().st_size
    except OSError:
        return {"text": "", "offset": 0, "size": 0}
    offset = max(0, min(offset, size))
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(min(limit, 262144))
    return {
        "text": chunk.decode("utf-8", "replace"),
        "offset": offset + len(chunk),
        "size": size,
    }
