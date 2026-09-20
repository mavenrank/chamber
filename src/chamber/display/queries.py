"""Read-only queries for the Chamber Console (Phase 1).

Desk × Console contract: this module is the SOLE history API. One function,
three transports — direct import (tests/CLI), the Desk `__chamberQuery`
binding, and `GET /api/query`. Console never touches the store directly.
See CHANGELOG 0.11.0.

Desk shows one live run. The Console needs the inverse: which sessions exist,
what one session did, which profile/browser it ran on, and which model served
which role. Everything here already exists — this module only repackages it
behind small JSON-safe functions so a WebView binding (or later a localhost
HTTP server) can call them without importing SQLite, paths, or discovery.

No browser is launched, no loop state is touched, nothing is written.
"""

from __future__ import annotations

from typing import Any


def _redacted_model(model: Any) -> dict[str, object]:
    """Public model identity only — never keys."""
    if model is None:
        return {}
    return {
        "provider": getattr(model, "provider", ""),
        "model": getattr(model, "model", ""),
        "base_url": getattr(model, "base_url", ""),
        "api_style": getattr(model, "api_style", ""),
        "reasoning_effort": getattr(model, "reasoning_effort", ""),
        "tool_calling": getattr(model, "tool_calling", True),
    }


def build_session_block(config: Any, build: Any, run_id: str) -> dict[str, object]:
    """Snapshot `session` block for `DisplayAdapter.set_session()`.

    Called once from `Chamber.start_display()`; survives `run_start` resets.
    """
    browser: dict[str, object] = {}
    if build is not None:
        browser = {
            "label": getattr(build, "label", str(build)),
            "family": getattr(build, "family", ""),
            "mv2": bool(getattr(build, "mv2", False)),
        }
    models: dict[str, object] = {}
    if config is not None:
        models = {
            "step": _redacted_model(getattr(config, "model", None)),
            "planner": _redacted_model(getattr(config, "orchestrator", None)),
            "vision": _redacted_model(getattr(config, "vision", None)),
            "vision_fallbacks": [
                _redacted_model(m) for m in (getattr(config, "vision_fallbacks", ()) or ())
            ],
            "display_mode": getattr(getattr(config, "display", None), "mode", ""),
        }
    return {
        "run_id": run_id,
        "profile": getattr(getattr(config, "browser", None), "profile", "")
        if config is not None
        else "",
        "browser": browser,
        "models": models,
    }


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent durable sessions, newest first. Read-only."""
    from chamber.trace.store import open_store

    with open_store() as store:
        return [dict(r) for r in store.runs(limit=limit)]


def get_run(run_id: str) -> dict[str, Any]:
    """One session: run + steps + actions + sources + exchanges grouped by role."""
    from chamber.trace.store import open_store

    with open_store() as store:
        run = store.run(run_id)
        if run is None:
            return {"run": None, "error": f"no such run: {run_id}"}
        exchanges = store.model_exchanges(run_id)
        by_role: dict[str, list[dict[str, Any]]] = {}
        for row in exchanges:
            by_role.setdefault(str(row.get("role", "model")), []).append(dict(row))
        models = sorted({str(r.get("model", "")) for r in exchanges if r.get("model")})
        return {
            "run": dict(run),
            "steps": [dict(s) for s in store.steps(run_id)],
            "actions": [dict(a) for a in store.actions(run_id)],
            "sources": [dict(s) for s in store.sources(run_id)],
            "exchanges_by_role": by_role,
            "exchange_count": len(exchanges),
            "models": models,
        }


def list_profiles() -> list[dict[str, Any]]:
    """Browser profiles on disk with size + login heuristic. Read-only."""
    from chamber import paths

    root = paths.home() / "profiles"
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*")):
        if not p.is_dir():
            continue
        total = 0
        try:
            for f in p.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
        cookies = p / "Default" / "Cookies"
        try:
            has_logins = cookies.exists() and cookies.stat().st_size > 20_000
        except OSError:
            has_logins = False
        out.append(
            {
                "name": p.name,
                "path": str(p),
                "size_mb": round(total / 1_048_576, 1),
                "logins": bool(has_logins),
            }
        )
    return out


def get_environment() -> dict[str, Any]:
    """Current `.env` config + autodetected browser. Best-effort, read-only."""
    from chamber.config import ChamberConfig

    env: dict[str, object] = {}
    try:
        cfg = ChamberConfig.from_env()
        env = {
            "profile": cfg.browser.profile,
            "models": {
                "step": _redacted_model(cfg.model),
                "planner": _redacted_model(cfg.orchestrator),
                "vision": _redacted_model(cfg.vision),
                "vision_fallbacks": [_redacted_model(m) for m in cfg.vision_fallbacks],
            },
            "display_mode": cfg.display.mode,
            "trace": cfg.trace,
        }
    except Exception as exc:  # config must never break the Console
        env = {"error": str(exc)[:300]}

    browser: dict[str, object] = {}
    try:
        from chamber.browser.discovery import discover_browser

        build = discover_browser()
        browser = {"label": build.label, "family": build.family, "mv2": build.mv2}
    except Exception as exc:
        browser = {"error": str(exc)[:300]}

    return {"config": env, "browser": browser, "profiles": list_profiles()}
