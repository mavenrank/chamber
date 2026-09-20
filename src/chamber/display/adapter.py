"""Normalize Chamber events into the state shown by Chamber Desk.

The loop, model client, vision sensor, and handoff code each speak slightly
different dialects.  The window should not know any of those wire formats, so this
module is the single adapter seam.  A provider-specific parser can be registered
later without changing the display renderer.

The adapter intentionally stores only information that was already visible to the
agent or returned by a provider: model responses, tool calls, parsed thoughts, and
API-provided reasoning summaries.  It does not attempt to obtain private hidden
chain-of-thought.
"""

from __future__ import annotations

import base64
import contextlib
import mimetypes
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_EVENTS = 360
MAX_TEXT = 8_000
MAX_IMAGE_BYTES = 1_500_000


def _clip(value: object, limit: int = MAX_TEXT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _role_label(role: object) -> str:
    return {
        "step": "Step model",
        "planner": "Planner",
        "vision": "Vision model",
    }.get(str(role), str(role or "Model").replace("_", " ").title())


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize() or "Activity"


def _safe(value: object, *, depth: int = 0) -> object:
    """Keep event details JSON-safe and bounded before they reach the browser."""
    if depth > 4:
        return _clip(value, 600)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _clip(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe(v, depth=depth + 1) for k, v in list(value.items())[:80]}
    if isinstance(value, (list, tuple, deque)):
        return [_safe(v, depth=depth + 1) for v in list(value)[:80]]
    return _clip(value)


def _request_details(payload: Mapping[str, Any]) -> dict[str, object]:
    request = payload.get("request")
    if not isinstance(request, Mapping):
        return {
            "prompt": _clip(payload.get("last_user", "")),
            "messages": payload.get("messages", 0),
            "tools": payload.get("tools", 0),
        }

    messages: list[dict[str, object]] = []
    raw_messages = request.get("messages", [])
    if isinstance(raw_messages, list):
        for message in raw_messages[:24]:
            if not isinstance(message, Mapping):
                continue
            messages.append(
                {
                    "role": str(message.get("role", "")),
                    "content": _clip(message.get("content", ""), 4_000),
                    "images": message.get("images", 0),
                }
            )

    tools: list[dict[str, object]] = []
    raw_tools = request.get("tools", [])
    if isinstance(raw_tools, list):
        for tool in raw_tools[:40]:
            if not isinstance(tool, Mapping):
                continue
            tools.append(
                {
                    "name": tool.get("name")
                    or (tool.get("function") or {}).get("name", "tool")
                    if isinstance(tool.get("function") or {}, Mapping)
                    else tool.get("name", "tool"),
                    "description": _clip(tool.get("description", ""), 600),
                }
            )

    return {
        "system": _clip(request.get("system", ""), 5_000),
        "messages": messages,
        "tools": tools,
    }


def _screenshot_data(path_value: object) -> str:
    if not path_value:
        return ""
    path = Path(str(path_value))
    try:
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


@dataclass(slots=True)
class DisplayState:
    """Durable in-process state for one Chamber task."""

    task: str = ""
    current: dict[str, str] = field(
        default_factory=lambda: {
            "title": "Waiting",
            "detail": "Ready for a task.",
            "tone": "neutral",
        }
    )
    step: str = ""
    max_steps: int = 0
    url: str = ""
    controlled: bool = False
    attention: dict[str, str] | None = None
    finished: bool = False
    success: bool = False
    events: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=MAX_EVENTS)
    )
    metrics: dict[str, int | float] = field(
        default_factory=lambda: {
            "actions_ok": 0,
            "actions_failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model_calls": 0,
            "vision_calls": 0,
        }
    )


@dataclass(slots=True, frozen=True)
class StatusSignal:
    """A raw status signal handed from a getter to a parser."""

    event: str
    payload: Mapping[str, Any]
    source: str = "loop"


@dataclass(slots=True)
class StatusUpdate:
    """The renderer-neutral result produced by a status parser."""

    title: str
    detail: str
    source: str
    tone: str = "neutral"
    step: str = ""
    meta: dict[str, object] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)
    screenshot_path: str = ""
    screenshot: str = ""
    state_title: str | None = None
    state_detail: str | None = None
    state_tone: str | None = None


# Internal parser names stay short while the public contract remains explicit.
_Normalized = StatusUpdate


class StatusGetter(Protocol):
    """Extract one status signal from a raw event, or return ``None``."""

    def get(self, event: str, payload: Mapping[str, Any]) -> StatusSignal | None: ...


class StatusParser(Protocol):
    """Turn one status signal into a display update, or return ``None``."""

    def parse(self, signal: StatusSignal) -> StatusUpdate | None: ...


@dataclass(slots=True)
class StatusAdapter:
    """A pluggable getter/parser pair for an additional event family."""

    getter: StatusGetter
    parser: StatusParser


Parser = Callable[[Mapping[str, Any]], StatusUpdate | None]


class DisplayAdapter:
    """Translate raw events into a stable, renderer-friendly snapshot."""

    def __init__(self, *, max_events: int = MAX_EVENTS) -> None:
        self.state = DisplayState()
        self.state.events = deque(maxlen=max_events)
        self._sequence = 0
        self._parsers: dict[str, Parser] = {}
        self._status_adapters: list[StatusAdapter] = []
        self._register_default_parsers()

    # ------------------------------------------------------------------ public

    def register(self, event: str, parser: Parser) -> None:
        """Register or replace the built-in parser for one event name."""
        self._parsers[event] = parser

    def register_status_adapter(
        self,
        getter: StatusGetter,
        parser: StatusParser,
        *,
        prepend: bool = True,
    ) -> StatusAdapter:
        """Slot a getter/parser pair into the live event path.

        Custom adapters are checked before the built-in Chamber events by default.
        Adding a new status therefore needs only three small pieces: a getter that
        recognizes the source event, a parser that returns :class:`StatusUpdate`,
        and this one registration call.  The window and the loop do not change.
        """
        adapter = StatusAdapter(getter, parser)
        if prepend:
            self._status_adapters.insert(0, adapter)
        else:
            self._status_adapters.append(adapter)
        return adapter

    def handle(self, event: str, payload: Mapping[str, Any] | None = None) -> dict[str, object]:
        """Apply an event and return the complete current display snapshot."""
        data = payload or {}
        if event == "run_start":
            self._reset_for_run(data)

        normalized: StatusUpdate | None = None
        for adapter in self._status_adapters:
            try:
                signal = adapter.getter.get(event, data)
                if signal is not None:
                    normalized = adapter.parser.parse(signal)
                    if normalized is not None:
                        break
            except Exception:
                # A third-party display adapter is optional instrumentation. Its
                # failure must not hide the built-in status or break the run.
                continue

        if normalized is None:
            parser = self._parsers.get(event, self._parse_unknown)
            try:
                normalized = parser(data)
            except Exception as exc:  # a display parser must never break the run
                normalized = StatusUpdate(
                    title=f"Could not read {event}",
                    detail=_clip(exc),
                    source="display",
                    tone="error",
                    state_title="Display update failed",
                    state_detail=_clip(exc),
                )

        if normalized is not None:
            self._apply(normalized, event, data)
        return self.snapshot()

    def get_status(self) -> dict[str, str]:
        """Return the current status line for embedders and tests."""
        return dict(self.state.current)

    def get_activity(self) -> list[dict[str, object]]:
        """Return a copy of the normalized activity chain."""
        return [dict(item) for item in self.state.events]

    def snapshot(self) -> dict[str, object]:
        """Return JSON-safe state for the companion window."""
        return {
            "task": self.state.task,
            "current": dict(self.state.current),
            "step": self.state.step,
            "max_steps": self.state.max_steps,
            "url": self.state.url,
            "controlled": self.state.controlled,
            "attention": dict(self.state.attention) if self.state.attention else None,
            "finished": self.state.finished,
            "success": self.state.success,
            "metrics": dict(self.state.metrics),
            "events": [dict(item) for item in self.state.events],
        }

    # -------------------------------------------------------------- state core

    def _reset_for_run(self, payload: Mapping[str, Any]) -> None:
        self.state.task = _clip(payload.get("task", ""), 2_000)
        self.state.current = {
            "title": "Starting",
            "detail": "Preparing the browser loop.",
            "tone": "active",
        }
        self.state.step = "0"
        self.state.max_steps = 0
        self.state.url = ""
        self.state.controlled = False
        self.state.attention = None
        self.state.finished = False
        self.state.success = False
        self.state.events.clear()
        for key in self.state.metrics:
            self.state.metrics[key] = 0

    def _apply(self, item: StatusUpdate, event: str, payload: Mapping[str, Any]) -> None:
        self._sequence += 1
        step = item.step or self.state.step
        if payload.get("step") is not None:
            step = str(payload["step"])
        if step:
            self.state.step = step
        if payload.get("max_steps") is not None:
            with contextlib.suppress(TypeError, ValueError):
                self.state.max_steps = int(payload["max_steps"])

        if item.state_title is not None:
            self.state.current = {
                "title": _clip(item.state_title, 160),
                "detail": _clip(item.state_detail if item.state_detail is not None else item.detail, 2_000),
                "tone": item.state_tone or item.tone,
            }
        self._update_context(event, payload)

        entry: dict[str, object] = {
            "id": self._sequence,
            "at": time.time(),
            "event": event,
            "title": _clip(item.title, 200),
            "detail": _clip(item.detail, MAX_TEXT),
            "source": _clip(item.source, 80),
            "tone": item.tone,
            "step": step,
            "meta": _safe(item.meta),
            "details": _safe(item.details),
        }
        if item.screenshot_path:
            entry["screenshot_path"] = item.screenshot_path
        if item.screenshot:
            entry["screenshot"] = item.screenshot
        self.state.events.append(entry)

    def _update_context(self, event: str, payload: Mapping[str, Any]) -> None:
        if event == "observe":
            self.state.url = _clip(payload.get("url", ""), 2_000)
        if event == "llm_request":
            self.state.metrics["model_calls"] += 1
        if event == "llm_response":
            self.state.metrics["input_tokens"] += _number(payload.get("input_tokens"))
            self.state.metrics["output_tokens"] += _number(payload.get("output_tokens"))
            if payload.get("role") == "vision":
                self.state.metrics["vision_calls"] += 1
        if event == "action":
            key = "actions_ok" if payload.get("ok") else "actions_failed"
            self.state.metrics[key] += 1
        if event == "controlled":
            self.state.controlled = bool(payload.get("taken"))
        if event == "human_request":
            self.state.attention = {
                "heading": _clip(payload.get("heading", "Your turn"), 200),
                "detail": _clip(payload.get("question", ""), 2_000),
            }
        elif event in ("human_resolved", "run_end"):
            self.state.attention = None
        if event == "run_end":
            self.state.finished = True
            self.state.success = bool(payload.get("success"))

    @staticmethod
    def _step(payload: Mapping[str, Any]) -> str:
        return str(payload.get("step", "")) if payload.get("step") is not None else ""

    # --------------------------------------------------------------- parsers

    def _register_default_parsers(self) -> None:
        for name in (
            "run_start",
            "observe",
            "plan",
            "llm_request",
            "llm_response",
            "thought",
            "action",
            "vision_request",
            "vision",
            "screenshot",
            "vision_ready",
            "stuck",
            "parse_retry",
            "controlled",
            "human_request",
            "human_resolved",
            "run_end",
        ):
            parser = getattr(self, f"_parse_{name}")
            self.register(name, parser)

    def _parse_run_start(self, p: Mapping[str, Any]) -> _Normalized:
        return _Normalized(
            title="Run started",
            detail=f"Working on {_clip(p.get('task', ''), 1_500)}",
            source="loop",
            tone="active",
            step="0",
            state_title="Starting",
            state_detail="Preparing the browser loop.",
            state_tone="active",
        )

    def _parse_observe(self, p: Mapping[str, Any]) -> _Normalized:
        url = _clip(p.get("url", ""), 2_000)
        controls = p.get("controls", 0)
        detail = f"Read {controls} controls"
        if url:
            detail += f" on {url}"
        return _Normalized(
            title="Page observed",
            detail=detail,
            source="browser",
            tone="neutral",
            step=self._step(p),
            meta={"url": url, "controls": controls, "tabs": p.get("tabs", [])},
            state_title="Viewing webpage",
            state_detail=detail,
            state_tone="active",
        )

    def _parse_plan(self, p: Mapping[str, Any]) -> _Normalized:
        stage = p.get("stage") or "the next stage"
        assessment = _clip(p.get("assessment", ""), 2_000)
        detail = f"Stage: {stage}"
        if assessment:
            detail += f" — {assessment}"
        return _Normalized(
            title="Plan updated",
            detail=detail,
            source="planner",
            tone="active",
            step=self._step(p),
            meta={"stage": stage, "current": p.get("current", 0)},
            details={
                "stages": p.get("stages", []),
                "current": p.get("current", 0),
                "assessment": assessment,
            },
            state_title="Planning",
            state_detail=detail,
            state_tone="active",
        )

    def _parse_llm_request(self, p: Mapping[str, Any]) -> _Normalized:
        role = _role_label(p.get("role"))
        model = _clip(p.get("model", "unknown model"), 200)
        images = int(p.get("images", 0) or 0)
        prompt = _clip(p.get("last_user", ""), 2_000)
        detail = f"{model} · {p.get('messages', 0)} messages"
        if images:
            detail += f" · {images} image{'s' if images != 1 else ''}"
        return _Normalized(
            title=f"{role} request",
            detail=detail,
            source=role,
            tone="active",
            meta={
                "model": model,
                "messages": p.get("messages", 0),
                "prompt_chars": p.get("prompt_chars", 0),
                "tools": p.get("tools", 0),
                "images": images,
            },
            details={"last_user_message": prompt, "request": _request_details(p)},
            state_title="Looking at screenshot" if images else "Thinking",
            state_detail=f"Asking {model}",
            state_tone="active",
        )

    def _parse_llm_response(self, p: Mapping[str, Any]) -> _Normalized:
        role = _role_label(p.get("role"))
        model = _clip(p.get("model", "unknown model"), 200)
        text = _clip(p.get("text", ""), 3_000)
        calls = p.get("tool_calls", [])
        call_names = [str(c.get("name", "tool")) for c in calls if isinstance(c, Mapping)]
        detail = text or ("Tool calls: " + ", ".join(call_names)) or "Empty response"
        summary = _clip(p.get("reasoning_summary", ""), 4_000)
        return _Normalized(
            title=f"{role} response",
            detail=detail,
            source=role,
            tone="active" if text or calls else "error",
            meta={
                "model": model,
                "seconds": round(float(p.get("seconds", 0) or 0), 2),
                "input_tokens": p.get("input_tokens", 0),
                "output_tokens": p.get("output_tokens", 0),
                "stop_reason": p.get("stop_reason", ""),
            },
            details={
                "response": text,
                "reasoning_summary": summary,
                "tool_calls": calls,
            },
            state_title="Reading screenshot" if p.get("role") == "vision" else "Thinking",
            state_detail=detail,
            state_tone="active" if text or calls else "error",
        )

    def _parse_thought(self, p: Mapping[str, Any]) -> _Normalized:
        text = _clip(p.get("text", ""), 4_000)
        repairs = p.get("repairs", [])
        source = _clip(p.get("source", "step model"), 80)
        if text:
            detail = text
            title = "Model-visible note" if source == "MCP" else "Model-visible thought"
            state_title = "Thinking"
        else:
            detail = "The response needed format repair."
            title = "Model response needs repair"
            state_title = "Repairing model response"
        if repairs:
            detail += f" Repairs: {', '.join(str(x) for x in repairs)}"
        return _Normalized(
            title=title,
            detail=detail,
            source=source,
            tone="active" if text else "attention",
            step=self._step(p),
            meta={"repairs": repairs},
            details={
                "text": text,
                "repairs": repairs,
                "visibility": "model-visible; not private hidden chain-of-thought",
            },
            state_title=state_title,
            state_detail=text or detail,
            state_tone="active" if text else "attention",
        )

    def _parse_action(self, p: Mapping[str, Any]) -> _Normalized:
        name = _clip(p.get("name", "action"), 160)
        detail = _clip(p.get("detail", ""), 2_500) or ("Completed" if p.get("ok") else "Failed")
        ok = bool(p.get("ok"))
        return _Normalized(
            title=f"Action · {name}",
            detail=detail,
            source="browser",
            tone="success" if ok else "error",
            step=self._step(p),
            meta={"action": name, "ok": ok},
            state_title="Acting",
            state_detail=f"{name}: {detail}",
            state_tone="active" if ok else "error",
        )

    def _parse_vision_request(self, p: Mapping[str, Any]) -> _Normalized:
        path = _clip(p.get("screenshot", ""), 2_000)
        question = _clip(p.get("question", "What is on screen?"), 3_000)
        return _Normalized(
            title="Vision request",
            detail=question,
            source="vision",
            tone="active",
            step=self._step(p),
            meta={"screenshot": path},
            details={"question": question, "screenshot_path": path},
            screenshot_path=path,
            screenshot=_screenshot_data(path),
            state_title="Viewing screenshot",
            state_detail=question,
            state_tone="active",
        )

    def _parse_vision(self, p: Mapping[str, Any]) -> _Normalized:
        path = _clip(p.get("screenshot", ""), 2_000)
        text = _clip(p.get("text", ""), 4_000)
        question = _clip(p.get("question", ""), 2_000)
        return _Normalized(
            title="Vision result",
            detail=text,
            source="vision",
            tone="active",
            step=self._step(p),
            meta={"screenshot": path},
            details={"question": question, "answer": text, "screenshot_path": path},
            screenshot_path=path,
            screenshot=_screenshot_data(path),
            state_title="Interpreting screenshot",
            state_detail=text,
            state_tone="active",
        )

    def _parse_screenshot(self, p: Mapping[str, Any]) -> _Normalized:
        path = _clip(p.get("screenshot", ""), 2_000)
        detail = f"Saved {path}" if path else "A screenshot was captured"
        return _Normalized(
            title="Screenshot captured",
            detail=detail,
            source="browser",
            tone="neutral",
            meta={"full_page": bool(p.get("full_page")), "scale": p.get("scale", 1)},
            details={"screenshot_path": path},
            screenshot_path=path,
            screenshot=_screenshot_data(path),
            state_title="Screenshot ready",
            state_detail=detail,
            state_tone="neutral",
        )

    def _parse_vision_ready(self, p: Mapping[str, Any]) -> _Normalized:
        loaded = bool(p.get("loaded"))
        detail = "The vision model is ready" if loaded else "It will load when a screenshot is needed"
        return _Normalized(
            title="Vision model ready" if loaded else "Vision model deferred",
            detail=detail,
            source="vision",
            tone="neutral" if loaded else "attention",
            meta={"seconds": round(float(p.get("seconds", 0) or 0), 2)},
            state_title="Waiting for a view" if loaded else "Vision will load on demand",
            state_detail=detail,
            state_tone="neutral" if loaded else "attention",
        )

    def _parse_stuck(self, p: Mapping[str, Any]) -> _Normalized:
        count = p.get("count", 0)
        detail = f"Nothing changed for {count} steps"
        detail += "; asking vision to inspect the screen" if p.get("vision") else "; nudging the model to read more"
        return _Normalized(
            title="Progress check",
            detail=detail,
            source="loop",
            tone="attention",
            step=self._step(p),
            meta={"count": count, "vision": bool(p.get("vision"))},
            state_title="Investigating a stall",
            state_detail=detail,
            state_tone="attention",
        )

    def _parse_parse_retry(self, p: Mapping[str, Any]) -> _Normalized:
        detail = _clip(p.get("error", "The model response was not readable."), 2_500)
        return _Normalized(
            title="Response repair",
            detail=f"Attempt {p.get('attempt', '?')}: {detail}",
            source="step model",
            tone="attention",
            step=self._step(p),
            meta={"attempt": p.get("attempt", 0)},
            state_title="Repairing model response",
            state_detail=detail,
            state_tone="attention",
        )

    def _parse_controlled(self, p: Mapping[str, Any]) -> _Normalized:
        taken = bool(p.get("taken"))
        waited = p.get("seconds")
        detail = "The browser is paused for you" if taken else "The agent is taking the browser back"
        if waited is not None:
            detail += f" after {float(waited):.1f}s"
        return _Normalized(
            title="Control paused" if taken else "Control returned",
            detail=detail,
            source="human",
            tone="attention" if taken else "active",
            step=self._step(p),
            state_title="Paused for you" if taken else "Resuming",
            state_detail=detail,
            state_tone="attention" if taken else "active",
        )

    def _parse_human_request(self, p: Mapping[str, Any]) -> _Normalized:
        heading = _clip(p.get("heading", "Your turn"), 200)
        question = _clip(p.get("question", ""), 3_000)
        resume = _clip(p.get("resume_when", ""), 1_000)
        detail = question
        if resume:
            detail += f" Resume when {resume}."
        return _Normalized(
            title=heading,
            detail=detail,
            source="human",
            tone="attention",
            details={"reason": p.get("reason", "blocked"), "resume_when": resume},
            state_title="Needs you",
            state_detail=detail,
            state_tone="attention",
        )

    def _parse_human_resolved(self, p: Mapping[str, Any]) -> _Normalized:
        resolved = bool(p.get("resolved"))
        detail = _clip(p.get("reason", "The page changed."), 1_500)
        waited = p.get("waited_s")
        if waited is not None:
            detail += f" after {float(waited):.1f}s"
        return _Normalized(
            title="Human step cleared" if resolved else "Human step timed out",
            detail=detail,
            source="human",
            tone="active" if resolved else "attention",
            state_title="Continuing" if resolved else "Continuing without input",
            state_detail=detail,
            state_tone="active" if resolved else "attention",
        )

    def _parse_run_end(self, p: Mapping[str, Any]) -> _Normalized:
        success = bool(p.get("success"))
        summary = _clip(p.get("summary", ""), 4_000)
        detail = summary or ("The task finished successfully." if success else "The task stopped.")
        return _Normalized(
            title="Task finished" if success else "Task stopped",
            detail=detail,
            source="loop",
            tone="success" if success else "attention",
            meta={
                "steps": p.get("steps", 0),
                "via_tools": p.get("via_tools", 0),
                "via_text": p.get("via_text", 0),
                "tool_rate": p.get("tool_rate", 0),
            },
            state_title="Finished" if success else "Stopped",
            state_detail="Task completed." if success else "Task stopped.",
            state_tone="success" if success else "attention",
        )

    def _parse_unknown(self, p: Mapping[str, Any]) -> _Normalized:
        return _Normalized(
            title="Activity",
            detail=_clip(" ".join(f"{k}: {v}" for k, v in p.items()), 3_000) or "Event received",
            source="loop",
            details={"payload": _safe(p)},
            state_title="Working",
            state_detail="An event was received.",
            state_tone="active",
        )


def _number(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
