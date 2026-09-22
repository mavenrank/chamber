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

import json
import re
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


def run_state(run_id: str) -> dict[str, Any]:
    """One-shot Live-view bootstrap: run row + pause flag + waiting notes."""
    from chamber import inbox as _box
    from chamber.trace.store import open_store

    with open_store() as store:
        run = store.run(_box.check_run_id(run_id))
    return {
        "run": run,
        "paused": _box.is_paused(run_id),
        "pending_notes": _box.pending_notes(run_id),
    }


# Step self-identification inside stored prompts. The step model is shown
# "# Step N of M" (prompt.py) and the planner "Step N of M. Currently on"
# (loop.py) — exact links, no timestamp guessing for these two roles.
_STEP_RE = re.compile(r"#?Step (\d+) of", re.IGNORECASE)

# Inline budgets: excerpts travel with the loop, bodies stay behind
# `exchange_detail` until clicked. See the read-path analysis (0.13.0).
_EXCERPT_CHARS = 1200
_DETAIL_CAP = 262144


def _exchange_step_n(role: str, request: dict[str, Any]) -> int | None:
    """Exact step number parsed from the stored prompt, if present."""
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        text = message.get("content", "")
        if not isinstance(text, str):
            continue
        match = _STEP_RE.search(text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _slim_request(request: dict[str, Any]) -> dict[str, Any]:
    """Counts plus the last user message excerpt — never whole prompts."""
    messages = request.get("messages") or []
    last_user = ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content", "")
            last_user = content if isinstance(content, str) else ""
            break
    tools = request.get("tools") or []
    images = sum(int(m.get("images", 0) or 0) for m in messages if isinstance(m, dict))
    return {
        "system_chars": len(request.get("system", "") or ""),
        "messages": len(messages),
        "tools": len(tools),
        "images": images,
        "last_user": last_user[:_EXCERPT_CHARS],
        "last_user_truncated": len(last_user) > _EXCERPT_CHARS,
    }


def _parse_plan(text: str) -> dict[str, Any] | None:
    """Forgiving read of the planner's JSON object (prose-tolerant)."""
    if not isinstance(text, str) or "{" not in text:
        return None
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        return None
    return {
        "assessment": str(data.get("assessment", "") or "")[:2000],
        "stages": [str(s) for s in data["stages"] if str(s).strip()][:12],
        "current": int(data.get("current", 0) or 0),
        "notes": str(data.get("notes", "") or "")[:4000],
        "done": bool(data.get("done", False)),
    }


def thought_loop(run_id: str, since_step: int = 0) -> dict[str, Any]:
    """The complete think → act loop, joined and budgeted.

    Steps carry their thought, slim step-model exchange, actions, the
    planner turn that set their stage, and nearby vision answers.
    Incremental: pass the last seen step to fetch only what is new.
    """
    from chamber.trace.store import open_store

    with open_store() as store:
        run = store.run(run_id)
        if run is None:
            return {"run": None, "error": f"no such run: {run_id}"}
        all_steps = store.steps(run_id)
        steps = [s for s in all_steps if s["n"] >= since_step]
        actions = [a for a in store.actions(run_id) if a["step_n"] >= since_step]
        exchanges = store.model_exchanges(run_id)
    total_steps = len(all_steps)

    acts: dict[int, list[dict[str, Any]]] = {}
    for a in actions:
        acts.setdefault(a["step_n"], []).append(
            {
                "name": a["name"],
                "args": _json_obj(a.get("args")),
                "why": a.get("why", ""),
                "outcome": a.get("outcome", ""),
                "message": (a.get("message", "") or "")[:800],
                "repairs": _json_list(a.get("repairs")),
                "ms": a.get("ms", 0),
            }
        )

    step_bounds = [(s["n"], s.get("at") or 0) for s in steps]
    cards = []
    planner_turns = []
    visions: list[dict[str, Any]] = []
    for x in exchanges:
        request = _json_obj(x.get("request_json"))
        response = _json_obj(x.get("response_json"))
        role = str(x.get("role", ""))
        if role == "planner":
            plan = _parse_plan(response.get("text", ""))
            planner_turns.append(
                {
                    "exchange_id": x["id"],
                    "started_at": x.get("started_at"),
                    "model": x.get("model", ""),
                    "ms": x.get("ms", 0),
                    "step_n": _exchange_step_n(role, request),
                    "plan": plan,
                    "raw_text": response.get("text", "")[:2000],
                }
            )
        elif role == "vision":
            visions.append(
                {
                    "exchange_id": x["id"],
                    "started_at": x.get("started_at"),
                    "model": x.get("model", ""),
                    "question": _vision_question(request),
                    "answer": str(response.get("text", ""))[:2000],
                }
            )
        else:
            cards.append(
                (_exchange_step_n(role, request), {
                    "exchange_id": x["id"],
                    "model": x.get("model", ""),
                    "ms": x.get("ms", 0),
                    "input_tokens": x.get("input_tokens", 0),
                    "output_tokens": x.get("output_tokens", 0),
                    "request": _slim_request(request),
                    "text": str(response.get("text", "")),
                    "reasoning_summary": str(response.get("reasoning_summary", ""))[:4000],
                    "tool_calls": response.get("tool_calls") or [],
                    "stop_reason": response.get("stop_reason", ""),
                })
            )

    # Active stage per step: latest planner turn at or before the step.
    turns_by_step = sorted(
        (t for t in planner_turns if t["step_n"] is not None), key=lambda t: t["step_n"]
    )
    cards_by_step: dict[int, dict[str, Any]] = {}
    for n, payload in cards:
        if n is not None:
            cards_by_step.setdefault(n, payload)

    out = []
    current_plan: dict[str, Any] | None = None
    for s in steps:
        n = s["n"]
        for t in turns_by_step:
            if t["step_n"] is not None and t["step_n"] <= n and t["plan"]:
                current_plan = t["plan"]
        out.append(
            {
                "n": n,
                "url": s.get("url", ""),
                "title": s.get("title", ""),
                "thought": s.get("thought", ""),
                "ms": s.get("ms", 0),
                "active_stage": _active_stage(current_plan),
                "exchange": cards_by_step.get(n),
                "actions": acts.get(n, []),
                "planner_turn": next(
                    (t for t in planner_turns if t["step_n"] == n and t["plan"]), None
                ),
                "vision": [v for v in visions if _in_window(v["started_at"], n, step_bounds)],
            }
        )
    return {"run": run, "steps": out, "total_steps": total_steps}


def exchange_detail(run_id: str, exchange_id: str) -> dict[str, Any]:
    """Full request/response bodies for one exchange, capped and flagged."""
    from chamber.trace.store import open_store

    with open_store() as store:
        rows = [
            r for r in store.model_exchanges(run_id) if str(r.get("id")) == str(exchange_id)
        ]
    if not rows:
        return {"exchange": None, "error": "no such exchange"}
    row = dict(rows[0])
    out = {}
    for key in ("request_json", "response_json"):
        raw = row.get(key) or "{}"
        out[key] = raw[:_DETAIL_CAP]
        out[key + "_truncated"] = len(raw) > _DETAIL_CAP
    return {
        "exchange": {
            "id": row.get("id"),
            "role": row.get("role"),
            "model": row.get("model"),
            "ms": row.get("ms"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            **out,
        }
    }


def _json_obj(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _vision_question(request: dict[str, Any]) -> str:
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                return content[:1000]
    return ""


def _active_stage(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan or not plan["stages"]:
        return None
    current = min(max(plan["current"], 0), len(plan["stages"]) - 1)
    return {"stage": plan["stages"][current], "current": current, "total": len(plan["stages"])}


def _in_window(
    started_at: object, n: int, bounds: list[tuple[int, float]]
) -> bool:
    """Vision has no step marker: nearest step window by timestamp."""
    try:
        at = float(started_at or 0)
    except (TypeError, ValueError):
        return False
    prev = 0.0
    for step_n, step_at in sorted(bounds):
        if step_n == n:
            return prev < at <= (step_at or float("inf"))
        prev = step_at or prev
    return False


# Restart-as-new budget: context injected as ONE mailbox note so the loop
# needs no new channel. Big enough to matter, small enough for cheap models.
_CONTEXT_CHARS = 3500


def restart_context(run_id: str) -> dict[str, Any]:
    """Everything a fresh run needs to continue a finished one.

    Planner notes + cited sources + recorded notes + open trail, capped.
    The caller files `context_note` first, then the user's steering note —
    mailbox order guarantees the model reads context before instruction.
    """
    from chamber import inbox as _box
    from chamber.trace.store import open_store

    try:
        _box.check_run_id(run_id)
    except ValueError as exc:
        return {"run": None, "error": str(exc)}
    with open_store() as store:
        run = store.run(run_id)
        if run is None:
            return {"run": None, "error": f"no such run: {run_id}"}
        recorded = store.notes(run_id)
        cited = [s["canonical"] for s in store.sources(run_id) if s.get("used")][:12]
        trail = [s["canonical"] for s in store.sources(run_id)][-5:]
        plan_notes = ""
        for x in reversed(store.model_exchanges(run_id)):
            if str(x.get("role", "")) != "planner":
                continue
            try:
                response = json.loads(x.get("response_json") or "{}")
            except (TypeError, ValueError):
                continue
            plan = _parse_plan(response.get("text", ""))
            if plan and plan["notes"].strip():
                plan_notes = plan["notes"]
                break
        chain: list[str] = []
        seen = {run_id}
        cursor = (run.get("parent_run") or "") if isinstance(run, dict) else ""
        while cursor and cursor not in seen and len(chain) < 10:
            seen.add(cursor)
            chain.append(cursor)
            parent = store.run(cursor)
            cursor = (parent.get("parent_run") or "") if parent else ""

    parts = [f"Continuing run {run_id}: {run.get('task', '')}"]
    if plan_notes:
        parts.append(f"Gathered so far:\n{plan_notes[:2000]}")
    if cited:
        parts.append("Trusted sources (do not re-crawl, cite these):\n" + "\n".join(cited))
    if recorded:
        prior = "\n".join(f"- {n.get('text', '')}" for n in recorded[-10:])
        parts.append(f"Earlier human notes:\n{prior}")
    if trail:
        parts.append("Last pages open:\n" + "\n".join(trail))
    context_note = "\n\n".join(parts)[:_CONTEXT_CHARS]
    return {
        "run": run,
        "task": run.get("task", ""),
        "parent_chain": chain,
        "context_note": context_note,
        "pending_notes": _box.pending_notes(run_id),
        "cited": cited,
    }


def thread(run_id: str) -> dict[str, Any]:
    """A run's thread: ancestors plus continuations, always fresh.

    Kept OUT of the cached `get_run` payload on purpose — children arrive
    after the parent ends, and an ended run's cache never refreshes.
    """
    from chamber import inbox as _box
    from chamber.trace.store import open_store

    try:
        _box.check_run_id(run_id)
    except ValueError as exc:
        return {"run": None, "error": str(exc)}
    with open_store() as store:
        run = store.run(run_id)
        if run is None:
            return {"run": None, "error": f"no such run: {run_id}"}
        ancestors: list[dict[str, Any]] = []
        seen = {run_id}
        cursor = run.get("parent_run") or ""
        while cursor and cursor not in seen and len(ancestors) < 10:
            seen.add(cursor)
            parent = store.run(cursor)
            if parent is None:
                break
            ancestors.append(
                {"id": parent["id"], "task": parent.get("task", ""),
                 "success": parent.get("success")}
            )
            cursor = parent.get("parent_run") or ""
        ancestors.reverse()
        return {
            "run_id": run_id,
            "parent": run.get("parent_run") or "",
            "ancestors": ancestors,
            "children": store.children(run_id),
        }


def usage_by_model() -> dict[str, Any]:
    """Per-model usage: runs, steps, tokens. Budget tracking reads this.

    Prices are deliberately absent — they rot. Free-tier models cost $0 by
    construction; paid usage is tokens × provider price, computed outside.
    """
    from chamber.trace.store import open_store

    with open_store() as store:
        rows = store.conn.execute(
            "SELECT model, COUNT(*) AS runs, SUM(input_tokens) AS in_tok,"
            " SUM(output_tokens) AS out_tok FROM run GROUP BY model"
        ).fetchall()
        steps = {
            r["model"]: r["steps"]
            for r in store.conn.execute(
                "SELECT run.model AS model, COUNT(step.id) AS steps FROM step"
                " JOIN run ON run.id = step.run_id GROUP BY run.model"
            ).fetchall()
        }
    out = []
    for r in rows:
        model = r["model"] or "(unknown)"
        out.append(
            {
                "model": model,
                "runs": r["runs"],
                "steps": steps.get(r["model"], 0),
                "input_tokens": r["in_tok"] or 0,
                "output_tokens": r["out_tok"] or 0,
                "free_tier": model.endswith(":free") or "mimo-v2.5-free" in model,
            }
        )
    out.sort(key=lambda r: (r["input_tokens"] or 0) + (r["output_tokens"] or 0), reverse=True)
    return {"models": out}


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
