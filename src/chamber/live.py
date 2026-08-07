"""A live view of the whole system, in boxes.

The browser window shows *what* the agent is doing. This shows *why*: which model
was asked what, what came back, how the planner and the step model and the vision
model hand work to each other, and where the data goes.

Laid out so each box answers one question:

    ┌─ chamber ─────────────────────────────────────── step 7/60 · 02:14 ─┐
    ├─ PLANNER ──────────────────┬─ STEP MODEL ────────────────────────────┤
    │ what is the plan, and      │ what was it asked, what did it say,     │
    │ which stage are we on      │ did it call a tool or write prose       │
    ├─ BROWSER ──────────────────┼─ VISION ────────────────────────────────┤
    │ url, tabs, controls        │ what the eyes were asked, what they saw │
    ├─ FLOW ─────────────────────┴─────────────────────────────────────────┤
    │ the running transcript: every request, response, action and result   │
    ├─ CLIPBOARD ──────────────────────────┬─ HEALTH ──────────────────────┤
    │ what has been copied                 │ tool rate, repairs, tokens    │
    └──────────────────────────────────────┴───────────────────────────────┘

Everything here is a *consumer* of events that already existed for the trace, plus
`llm_request`/`llm_response` which were added for this. Nothing in the agent knows
the view exists — it is a listener, so it cannot slow the run down or break it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# One accent per participant, used consistently everywhere so you can follow a
# single actor down the transcript by colour alone.
PLANNER = "blue"
STEPPER = "cyan"
VISION = "magenta"
BROWSER = "green"
HUMAN = "yellow"


def _wrap(text: str, width: int = 200) -> str:
    return " ".join((text or "").split())[:width]


@dataclass
class LiveState:
    """Everything the view needs. Plain data, updated by events."""

    task: str = ""
    started: float = field(default_factory=time.monotonic)
    step: int = 0
    max_steps: int = 0

    # planner
    stages: list[str] = field(default_factory=list)
    current_stage: int = 0
    assessment: str = ""
    planner_calls: int = 0

    # step model
    last_ask: str = ""
    last_reply: str = ""
    last_tool: str = ""
    reply_seconds: float = 0.0
    thought: str = ""

    # vision
    vision_question: str = ""
    vision_answer: str = ""
    vision_seconds: float = 0.0

    # browser
    url: str = ""
    controls: int = 0
    tabs: list[dict[str, str]] = field(default_factory=list)
    clips: list[str] = field(default_factory=list)

    # health
    via_tools: int = 0
    via_text: int = 0
    repairs: int = 0
    actions_ok: int = 0
    actions_failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    stuck: int = 0

    flow: deque[tuple[str, str, str]] = field(default_factory=lambda: deque(maxlen=200))
    finished: str = ""
    success: bool = False

    def log(self, who: str, colour: str, what: str) -> None:
        self.flow.append((who, colour, what))

    @property
    def elapsed(self) -> str:
        s = int(time.monotonic() - self.started)
        return f"{s // 60:02d}:{s % 60:02d}"

    @property
    def tool_rate(self) -> float:
        n = self.via_tools + self.via_text
        return self.via_tools / n if n else 0.0


class LiveView:
    """Renders `LiveState`. Feed it events with `handle`."""

    def __init__(self, console: Console | None = None, *, flow_lines: int = 14) -> None:
        self.state = LiveState()
        self.console = console or Console()
        self.flow_lines = flow_lines
        self._live: Live | None = None

    # ------------------------------------------------------------------ events

    def handle(self, event: str, payload: dict[str, Any]) -> None:
        """The listener. Never raises — a broken view must not end a run."""
        try:
            self._handle(event, payload)
            if self._live is not None:
                self._live.update(self.render())
        except Exception:
            pass

    def _handle(self, event: str, p: dict[str, Any]) -> None:
        s = self.state

        if event == "run_start":
            s.task = p["task"]
            s.log("run", "white", f"task: {_wrap(p['task'], 160)}")

        elif event == "observe":
            s.step = p["step"]
            s.url = p["url"]
            s.controls = p["controls"]
            s.log("browser", BROWSER, f"observed {p['controls']} controls | {_wrap(p['url'], 90)}")

        elif event == "plan":
            s.stages = p.get("stages", [])
            s.current_stage = p.get("current", 0)
            s.assessment = p.get("assessment", "")
            s.planner_calls += 1
            stage = s.stages[s.current_stage] if s.stages else ""
            s.log("planner", PLANNER, f"stage {s.current_stage + 1}/{len(s.stages)}: {_wrap(stage, 110)}")

        elif event == "llm_request":
            role = p.get("role", "model")
            colour = PLANNER if role == "planner" else VISION if role == "vision" else STEPPER
            bits = [f"{p['prompt_chars']:,} chars"]
            if p.get("tools"):
                bits.append(f"{p['tools']} tools")
            if p.get("images"):
                bits.append(f"{p['images']} image(s)")
            s.log(f"{role} >", colour, f"ask {p['model']} ({', '.join(bits)})")
            if role == "step":
                s.last_ask = p.get("last_user", "")
            elif role == "vision":
                s.vision_question = p.get("last_user", "")

        elif event == "llm_response":
            role = p.get("role", "model")
            colour = PLANNER if role == "planner" else VISION if role == "vision" else STEPPER
            s.tokens_in += p.get("input_tokens", 0)
            s.tokens_out += p.get("output_tokens", 0)
            calls = p.get("tool_calls") or []
            if calls:
                shown = ", ".join(
                    f"{c['name']}({', '.join(f'{k}={v!r}' for k, v in list(c['arguments'].items())[:2])})"
                    for c in calls[:2]
                )
                s.log(f"{role} <", colour, f"{p['seconds']:.1f}s | TOOL {_wrap(shown, 110)}")
            else:
                s.log(f"{role} <", colour, f"{p['seconds']:.1f}s | text {_wrap(p.get('text', ''), 110)}")
            if role == "step":
                s.last_reply = p.get("text", "")
                s.last_tool = calls[0]["name"] if calls else ""
                s.reply_seconds = p.get("seconds", 0.0)
            elif role == "vision":
                s.vision_answer = p.get("text", "")
                s.vision_seconds = p.get("seconds", 0.0)

        elif event == "thought":
            s.thought = p.get("text", "")
            if s.thought:
                s.log("step", STEPPER, f'"{_wrap(s.thought, 120)}"')

        elif event == "action":
            if p["ok"]:
                s.actions_ok += 1
            else:
                s.actions_failed += 1
            mark = "ok " if p["ok"] else "ERR"
            s.log("act", BROWSER if p["ok"] else "red", f"{mark} {p['name']}: {_wrap(p['detail'], 110)}")

        elif event == "vision":
            s.vision_answer = p.get("text", "")
            s.log("vision", VISION, f"saw: {_wrap(p.get('text', ''), 120)}")

        elif event == "stuck":
            s.stuck = p.get("count", 0)
            where = "asking vision" if p.get("vision") else "nudging to read more"
            s.log("loop", HUMAN, f"no change for {s.stuck} steps — {where}")

        elif event == "parse_retry":
            s.repairs += 1
            s.log("repair", HUMAN, f"reply unreadable (attempt {p['attempt']}): {_wrap(p['error'], 100)}")

        elif event == "vision_ready":
            s.log("vision", VISION, f"model resident ({p['seconds']:.1f}s)")

        elif event == "run_end":
            s.finished = p.get("summary", "")
            s.success = p.get("success", False)
            s.via_tools = p.get("via_tools", s.via_tools)
            s.via_text = p.get("via_text", s.via_text)
            s.log("run", "white", "finished" if s.success else "stopped without finishing")

    # ----------------------------------------------------------------- panels

    def _planner_panel(self) -> Panel:
        s = self.state
        if not s.stages:
            body: Any = Text("no planner configured — the step model plans for itself", style="dim")
        else:
            lines = []
            for i, stage in enumerate(s.stages):
                if i < s.current_stage:
                    lines.append(Text(f" [x] {_wrap(stage, 60)}", style="dim green"))
                elif i == s.current_stage:
                    lines.append(Text(f" >>> {_wrap(stage, 60)}", style=f"bold {PLANNER}"))
                else:
                    lines.append(Text(f" [ ] {_wrap(stage, 60)}", style="dim"))
            if s.assessment:
                lines.append(Text(""))
                lines.append(Text(_wrap(s.assessment, 140), style="italic dim"))
            body = Group(*lines)
        return Panel(body, title=f"[{PLANNER}]PLANNER[/{PLANNER}]",
                     subtitle=f"{s.planner_calls} calls", border_style=PLANNER)

    def _stepper_panel(self) -> Panel:
        s = self.state
        rows = []
        if s.thought:
            rows.append(Text(_wrap(s.thought, 220), style="italic"))
            rows.append(Text(""))
        if s.last_tool:
            rows.append(Text(f"-> tool call: {s.last_tool}", style=f"bold {STEPPER}"))
        elif s.last_reply:
            rows.append(Text(f"-> text reply: {_wrap(s.last_reply, 150)}", style=HUMAN))
        if s.reply_seconds:
            rows.append(Text(f"  {s.reply_seconds:.1f}s", style="dim"))
        if not rows:
            rows = [Text("waiting…", style="dim")]
        return Panel(Group(*rows), title=f"[{STEPPER}]STEP MODEL[/{STEPPER}]",
                     subtitle=f"{s.tool_rate:.0%} via tools", border_style=STEPPER)

    def _browser_panel(self) -> Panel:
        s = self.state
        rows = [Text(_wrap(s.url, 120) or "(nothing loaded)", style=BROWSER),
                Text(f"{s.controls} controls on the page", style="dim")]
        if s.tabs:
            rows.append(Text(""))
            for t in s.tabs[:5]:
                purpose = f"  ({t['purpose']})" if t.get("purpose") else ""
                rows.append(Text(f" [{t['id']}] {_wrap(t.get('url',''), 60)}{purpose}", style="dim"))
        return Panel(Group(*rows), title=f"[{BROWSER}]BROWSER[/{BROWSER}]",
                     subtitle=f"{len(s.tabs) or 1} tab(s)", border_style=BROWSER)

    def _vision_panel(self) -> Panel:
        s = self.state
        if not s.vision_answer and not s.vision_question:
            body: Any = Text("not consulted yet — text is tried first", style="dim")
        else:
            rows = []
            if s.vision_question:
                rows.append(Text(f"asked: {_wrap(s.vision_question, 150)}", style="dim"))
            if s.vision_answer:
                rows.append(Text(""))
                rows.append(Text(_wrap(s.vision_answer, 220), style=VISION))
            body = Group(*rows)
        sub = f"{s.vision_seconds:.1f}s" if s.vision_seconds else "idle"
        return Panel(body, title=f"[{VISION}]VISION[/{VISION}]", subtitle=sub, border_style=VISION)

    def _flow_panel(self) -> Panel:
        s = self.state
        table = Table.grid(padding=(0, 1))
        table.add_column(width=10, no_wrap=True)
        table.add_column(overflow="ellipsis")
        for who, colour, what in list(s.flow)[-self.flow_lines:]:
            table.add_row(Text(who, style=colour), Text(what))
        return Panel(table, title="FLOW", border_style="white", subtitle="most recent last")

    def _health_panel(self) -> Panel:
        s = self.state
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="dim")
        t.add_column()
        rate_style = "green" if s.tool_rate > 0.9 else HUMAN if s.tool_rate > 0.5 else "red"
        t.add_row("tool rate", Text(f"{s.tool_rate:.0%}", style=rate_style))
        t.add_row("decisions", f"{s.via_tools} tool / {s.via_text} text")
        t.add_row("repairs", Text(str(s.repairs), style=HUMAN if s.repairs else "green"))
        t.add_row("actions", Text(f"{s.actions_ok} ok / {s.actions_failed} failed",
                                  style="red" if s.actions_failed else "green"))
        t.add_row("tokens", f"{s.tokens_in:,} in / {s.tokens_out:,} out")
        if s.stuck:
            t.add_row("stuck", Text(f"{s.stuck} steps", style=HUMAN))
        return Panel(t, title="HEALTH", border_style="white")

    def _clipboard_panel(self) -> Panel:
        s = self.state
        if not s.clips:
            body: Any = Text("empty", style="dim")
        else:
            body = Group(*[Text(f" - {_wrap(c, 90)}", style="dim") for c in s.clips[-5:]])
        return Panel(body, title="CLIPBOARD",
                     subtitle=f"{len(s.clips)} clip(s)", border_style="white")

    def render(self) -> Layout:
        """Lay the panels out for the terminal we actually have.

        Fixed-size rows do not shrink in rich, so hard-coding them means the last
        row — the transcript, the most useful part — silently collapses to zero
        height on a short terminal. Sizes are derived from the real height and the
        transcript takes whatever is left, with a floor.
        """
        height = max(self.console.size.height, 24)
        # Budget: header + two panel rows + footer, transcript gets the remainder.
        pair_h = 9 if height >= 44 else 7 if height >= 34 else 6
        foot_h = 8 if height >= 40 else 6
        flow_h = max(height - 3 - (pair_h * 2) - foot_h, 5)
        self.flow_lines = max(flow_h - 2, 3)

        root = Layout()
        root.split_column(
            Layout(self._header(), size=3, name="head"),
            Layout(name="top", size=pair_h),
            Layout(name="mid", size=pair_h),
            Layout(self._flow_panel(), name="flow", size=flow_h),
            Layout(name="foot", size=foot_h),
        )
        root["top"].split_row(Layout(self._planner_panel()), Layout(self._stepper_panel()))
        root["mid"].split_row(Layout(self._browser_panel()), Layout(self._vision_panel()))
        root["foot"].split_row(
            Layout(self._clipboard_panel()), Layout(self._health_panel(), size=42)
        )
        return root

    def _header(self) -> Panel:
        s = self.state
        step = f"step {s.step}/{s.max_steps}" if s.max_steps else f"step {s.step}"
        return Panel(
            Text(_wrap(s.task, 200) or "…", style="bold"),
            title="chamber",
            subtitle=f"{step} | {s.elapsed}",
            border_style="green" if s.success else "white",
        )

    # ------------------------------------------------------------------ driving

    def __enter__(self) -> LiveView:
        self._live = Live(self.render(), console=self.console, refresh_per_second=6,
                          screen=False, transient=False)
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.update(self.render())
            self._live.__exit__(*exc)
            self._live = None
