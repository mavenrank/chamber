"""Where chamber keeps things on disk.

One root, overridable with CHAMBER_HOME. Everything under it is disposable except
`profiles/`, which holds real cookies and logins — see `.gitignore`.

    ~/.chamber/
      profiles/<name>/        chromium user-data-dir (the whole browser profile)
      extensions/             unpacked extensions, e.g. uBlock0.chromium/
      runs/<run_id>/          per-run artifacts: screenshots, traces, har
      chamber.sqlite          run/step/action trace
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Chamber's state root. Created on first access."""
    raw = os.environ.get("CHAMBER_HOME")
    root = Path(raw).expanduser() if raw else Path.home() / ".chamber"
    root.mkdir(parents=True, exist_ok=True)
    return root


def profile_dir(name: str = "default") -> Path:
    """user-data-dir for a named profile.

    Profiles are per-purpose on purpose: a `research` profile with no logins and a
    `dev` profile carrying your app's session behave very differently, and you do
    not want an agent doing open-ended research inside a logged-in profile.
    """
    p = home() / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def extensions_dir() -> Path:
    p = home() / "extensions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_dir(run_id: str) -> Path:
    p = home() / "runs" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "chamber.sqlite"
