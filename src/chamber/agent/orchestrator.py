"""The planner: a strong model that thinks rarely, so a cheap one can act often.

A single-model loop asks one model to do two very different jobs on every step —
hold the whole task in its head *and* pick the next click. That is expensive when
the model is good and unreliable when it is cheap, which is why a long task with a
small model drifts: by step thirty it has forgotten there were five laptops.

So the work is split by *frequency*:

**The orchestrator** (strong, slow, expensive) is called a handful of times per run
— at the start, when a stage finishes, when the loop is stuck, and periodically as
a check-in. It never sees the control list and never emits an action. It maintains a
short plan of stages and says which one is current.

**The step model** (cheap, fast) is called every step. It sees the page and one
stage, not the whole task. "Add the third laptop to the cart" is a job a 3B model
does well; "comparison-shop across two countries and then ask ChatGPT" is not.

The saving is real: on the Amazon task the planner would run maybe six times
against the step model's fifty.

**State belongs outside the model.** The plan lives here, page state lives in tabs,
and text lives in the clipboard. Anything the model has to remember is something it
can forget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from chamber.agent.llm import LLM, LLMError, Message

log = logging.getLogger(__name__)

_SYSTEM = """\
You are the planner for a browser agent. You do not touch the browser yourself — a
separate, smaller model does that, one action at a time, and it only ever sees the
current page plus the single stage you give it.

Your job is to break the task into stages and track which one is current.

Rules that make the difference:

- **Stages are outcomes, not clicks.** "Record the top 5 laptops from amazon.in with
  their prices" is a stage. "Click the third link" is not — that is the step model's
  business, and it can see the page.
- **Keep it to 3-7 stages.** More than that and the plan is doing the step model's
  job for it.
- **Use tabs and the clipboard as memory.** The step model forgets things between
  steps; a tab left open on the cart, or a labelled clip holding a product title,
  does not. Say so in the stage when it matters — "open amazon.com in a second tab
  and leave the India tab open".
- **`notes` is the run's memory.** Anything gathered that later stages need — prices,
  names, an exchange rate — put it there. It is carried forward verbatim and is the
  only thing that survives the step model's short memory.
- **When a stage is clearly done, move on.** When every stage is done, set `done`.

Reply with one JSON object and nothing else:

{"assessment": "one sentence on where things stand",
 "stages": ["...", "..."],
 "current": 0,
 "notes": "facts gathered so far that later stages need",
 "done": false}
"""


@dataclass(slots=True)
class Plan:
    stages: list[str] = field(default_factory=list)
    current: int = 0
    notes: str = ""
    assessment: str = ""
    done: bool = False

    @property
    def stage(self) -> str:
        if self.done or not self.stages:
            return ""
        return self.stages[min(self.current, len(self.stages) - 1)]

    def render(self) -> str:
        """What the step model is shown — its stage, with the rest as context.

        The whole plan is included rather than just the current stage: knowing what
        comes next stops the step model from finishing prematurely, and knowing what
        is behind it stops it from redoing work.
        """
        if not self.stages:
            return ""
        lines = ["## Your current stage", ""]
        for i, stage in enumerate(self.stages):
            if i < self.current:
                lines.append(f"  [done] {stage}")
            elif i == self.current:
                lines.append(f"  ➤ NOW: {stage}")
            else:
                lines.append(f"  [later] {stage}")
        lines.append("")
        lines.append(
            "Work only on the stage marked NOW. Do not call `done` until every "
            "stage is finished — the planner will tell you when that is."
        )
        if self.notes:
            lines.append("")
            lines.append("## What has been gathered so far")
            lines.append(self.notes)
        return "\n".join(lines)


class Orchestrator:
    """Maintains the plan. Called rarely, on purpose."""

    def __init__(self, llm: LLM, *, every: int = 8) -> None:
        self.llm = llm
        self.every = every
        self.plan = Plan()
        self.calls = 0
        self._last_call_step = 0

    def should_replan(self, step: int, *, stuck: bool, errored: bool) -> bool:
        """Cheap heuristics, deliberately not a model call.

        Replanning is the expensive path, so the decision to do it must not itself
        cost a request.
        """
        if not self.plan.stages:
            return True
        if stuck or errored:
            return True
        return step - self._last_call_step >= self.every

    async def replan(self, task: str, *, step: int, situation: str) -> Plan:
        """Ask the planner where things stand and what happens next."""
        self.calls += 1
        self._last_call_step = step

        current = (
            json.dumps(
                {
                    "stages": self.plan.stages,
                    "current": self.plan.current,
                    "notes": self.plan.notes,
                },
                ensure_ascii=False,
            )
            if self.plan.stages
            else "(no plan yet — this is the start of the run)"
        )

        prompt = (
            f"# The task\n\n{task}\n\n"
            f"# Your current plan\n\n{current}\n\n"
            f"# What has happened since\n\n{situation}\n\n"
            "Update the plan. If the current stage is finished, advance `current`. "
            "Carry everything gathered so far into `notes` — it is the only memory "
            "the step model has."
        )

        try:
            response = await self.llm.complete(_SYSTEM, [Message("user", prompt)])
        except LLMError as exc:
            # A planner failure must not end the run: the existing plan is still
            # good, and a run with a stale plan beats no run at all.
            log.warning("planner unavailable, keeping the existing plan: %s", exc)
            return self.plan

        parsed = _parse(response.text)
        if parsed is None:
            log.info("planner reply unreadable, keeping the existing plan")
            return self.plan

        # Never let the plan go backwards or lose gathered notes — a planner having
        # an off turn should not erase what the run already learned.
        if parsed.notes.strip():
            self.plan.notes = parsed.notes
        if parsed.stages:
            self.plan.stages = parsed.stages
        self.plan.current = max(self.plan.current, parsed.current) if parsed.stages else 0
        self.plan.current = min(self.plan.current, max(len(self.plan.stages) - 1, 0))
        self.plan.assessment = parsed.assessment
        self.plan.done = parsed.done
        return self.plan


def _parse(text: str) -> Plan | None:
    """Read the planner's reply, reusing the same forgiving JSON extraction the
    action parser uses — a planner emits prose around its JSON just as readily."""
    from chamber.agent.parse import _json_blobs, _load

    for blob in _json_blobs(text):
        data = _load(blob)
        if not isinstance(data, dict):
            continue
        stages = data.get("stages")
        if not isinstance(stages, list):
            continue
        return Plan(
            stages=[str(s) for s in stages if str(s).strip()][:12],
            current=int(data.get("current", 0) or 0),
            notes=str(data.get("notes", "") or ""),
            assessment=str(data.get("assessment", "") or ""),
            done=bool(data.get("done", False)),
        )
    return None
