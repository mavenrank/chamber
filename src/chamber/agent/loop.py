"""observe → decide → act → repeat.

The loop is deliberately thin. Everything hard lives one layer down — extraction in
`dom/`, durability in `actions/executor.py`, format tolerance in `agent/parse.py` —
so what is left here is scheduling and the three judgement calls the loop alone can
make:

**When to stop.** `done`, the step budget, or too many consecutive errors. Running
out of steps is treated as a failure with a partial answer, not a crash: whatever
the agent learned before the budget ran out is usually worth having.

**What the model remembers.** Full observations are heavy, and every past one stays
in context forever if you let it. Only the last few are kept in full; older turns
collapse to a line each. This is not just a cost decision — a model shown six
near-identical page dumps starts confusing which one is current, and picking refs
from an old one is the single most common way an agent goes off the rails.

**When it is stuck.** If the page fingerprint has not moved for several steps, the
agent is doing something that has no effect and cannot tell. The loop says so
explicitly and attaches a screenshot, because "nothing you did changed anything" is
information the observation itself cannot carry.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from chamber.actions.result import ActionResult, format_batch
from chamber.actions.schema import Done, tool_schemas
from chamber.agent import parse, prompt
from chamber.agent.llm import LLM, LLMError, Message
from chamber.agent.orchestrator import Orchestrator
from chamber.dom import serialize
from chamber.session import Chamber
from chamber.trace.store import TraceStore

log = logging.getLogger(__name__)

# How many past turns keep their full observation. Two is enough to compare "before
# and after" without letting stale refs pile up.
_FULL_HISTORY = 2


@dataclass(slots=True)
class Step:
    n: int
    thought: str = ""
    actions: list[str] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    url: str = ""
    fingerprint: str = ""
    ms: int = 0
    repairs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def one_line(self) -> str:
        verbs = ", ".join(self.actions) or "(nothing)"
        mark = "✓" if self.ok else "✗"
        return f"step {self.n}: {mark} {verbs} @ {self.url[:60]}"


@dataclass(slots=True)
class RunResult:
    task: str
    success: bool
    summary: str
    steps: list[Step]
    elapsed_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_because: str = ""

    @property
    def step_count(self) -> int:
        return len(self.steps)


# Called with (event_name, payload) — the CLI renders these, the MCP server
# forwards them as progress notifications.
Listener = Callable[[str, dict[str, Any]], None]

# Contributes extra text to a step's input, given the session and the fresh
# observation. This is the seam an embedder uses to put domain knowledge in front of
# the model without teaching chamber the domain: work out whatever your application
# knows — a classification of the page, what you have already collected, a warning
# about where this link goes — and return it as prose.
#
# Preferred over adding actions for the same purpose. An action costs a step every
# time the model chooses to call it and can be forgotten; an annotation is simply
# always there. Returning "" adds nothing.
Annotator = Callable[["Chamber", Any], Awaitable[str]]


class Agent:
    """Drives a `Chamber` with a model until the task is done."""

    def __init__(
        self,
        chamber: Chamber,
        llm: LLM,
        *,
        on_event: Listener | None = None,
        system_extra: str = "",
        store: TraceStore | None = None,
        orchestrator: Orchestrator | None = None,
        annotate: Annotator | None = None,
    ) -> None:
        self.ch = chamber
        self.annotate = annotate
        self.llm = llm
        self.on_event = on_event or (lambda _e, _p: None)
        self.store = store
        self.orchestrator = orchestrator
        self.system = prompt.system_prompt(
            tool_calling=llm.config.tool_calling, extra=system_extra
        )
        self._tools = tool_schemas() if llm.config.tool_calling else None
        # How decisions arrived. Reported at the end of a run: a low tool rate with
        # tools offered means the model is choosing prose, which is a different
        # problem from the endpoint not supporting them.
        self.via_tools = 0
        self.via_text = 0

    def _emit(self, event: str, **payload: Any) -> None:
        try:
            self.on_event(event, payload)
        except Exception:
            log.debug("event listener raised", exc_info=True)

    # -------------------------------------------------------------------- run

    async def run(self, task: str, *, start_url: str | None = None) -> RunResult:
        cfg = self.ch.config.loop
        started = time.monotonic()
        self.ch.goal = task

        history: list[Message] = [Message("user", prompt.task_prompt(task))]
        steps: list[Step] = []
        feedback = ""
        consecutive_errors = 0
        stuck_count = 0
        last_fingerprint = ""
        last_visit_url = ""
        stopped_because = "step budget exhausted"
        summary = ""
        success = False

        await self.ch.overlay.think(goal=task, thought="Starting.", status="busy", step="0")
        self._emit("run_start", task=task)

        # Load the vision model now rather than mid-run. The first stuck moment is
        # the worst possible time to discover it needs 90 seconds to come off disk,
        # and `keep_alive` then holds it in VRAM for the rest of the run.
        if self.ch.vision is not None:
            await self.ch.overlay.think(thought="Preparing the vision model…")
            warmed = await self.ch.vision.warm()
            if warmed is not None:
                loaded, took = warmed
                self._emit("vision_ready", loaded=loaded, seconds=took)

        if self.store:
            self.store.start_run(
                self.ch.run_id,
                task,
                profile=self.ch.config.profile,
                model=f"{self.llm.config.provider}/{self.llm.config.model}",
            )

        if start_url:
            await self.ch.goto(start_url)

        for n in range(1, cfg.max_steps + 1):
            step_started = time.monotonic()
            step = Step(n=n)

            # --- yield to the human ----------------------------------------
            # Checked before anything else in the step. Pressing Take Control
            # stops the agent here, not after it finishes whatever it was doing —
            # which is the difference between a handover and a progress bar.
            if self.ch.controlled:
                self._emit("controlled", step=n, taken=True)
                waited = await self.ch.wait_while_controlled()
                self._emit("controlled", step=n, taken=False, seconds=waited)
                # The person almost certainly navigated or clicked while they had
                # it, so every remembered ref is meaningless now.
                self.ch.last_snapshot = None

            # --- observe ---------------------------------------------------
            snap = await self.ch.observe()
            step.url = snap.url
            step.fingerprint = serialize.fingerprint(snap)

            if step.fingerprint == last_fingerprint:
                stuck_count += 1
            else:
                stuck_count = 0
            last_fingerprint = step.fingerprint

            if stuck_count >= cfg.stuck_after:
                note = (
                    f"Nothing on the page has changed for {stuck_count} steps. "
                    "Whatever you are doing is having no effect."
                )
                use_vision = (
                    self.ch.vision is not None
                    and cfg.screenshot_on_stuck
                    and stuck_count >= cfg.vision_after
                )

                if not use_vision:
                    # Text first. The structural view usually does hold the answer,
                    # and a local vision call costs ~19s — so the model gets a
                    # couple of steps to find it itself before we spend that.
                    note += (
                        " Before anything else, read what is already on the page: "
                        "call read_page with a large budget (16000+), and "
                        "whole_page=true if this is a list or results page. If you "
                        "have already done that, try a different element, scroll, "
                        "or go back — repeating the same action will not help."
                    )
                else:
                    path = await self.ch.screenshot(
                        name=f"stuck-step{n}.png", scale=cfg.vision_scale
                    )
                    # The one moment an image reliably beats the structure: after
                    # the model has had its chances with text and still cannot see
                    # what is in the way. Described in words, so the planner needs
                    # no vision of its own.
                    seen = await self.ch.vision.why_stuck(path, task)
                    note += f"\n\nI had a vision model look at the screen: {seen}"
                    self._emit("vision", step=n, text=seen)

                feedback = f"{feedback}\n\n{note}" if feedback else note
                self._emit("stuck", step=n, count=stuck_count, vision=use_vision)

            await self.ch.overlay.think(step=f"{n}/{cfg.max_steps}", status="busy")
            self._emit(
                "observe",
                step=n,
                url=snap.url,
                controls=len(snap.elements),
                tabs=self.ch.tab_summary(),
                clips=[c.preview(80) for c in self.ch.clipboard],
                max_steps=cfg.max_steps,
            )

            if self.store and snap.url != last_visit_url:
                # One row per page the agent actually looked at, not per step —
                # three steps on the same page is one source, not three.
                self.store.record_visit(
                    snap.url,
                    step_n=n,
                    title=snap.title,
                    content_chars=len(snap.content),
                    controls=len(snap.elements),
                )
                last_visit_url = snap.url

            # --- plan ------------------------------------------------------
            plan_block = ""
            if self.orchestrator is not None:
                errored = consecutive_errors > 0
                if self.orchestrator.should_replan(n, stuck=stuck_count > 0, errored=errored):
                    situation = (
                        f"Step {n} of {cfg.max_steps}. Currently on {snap.url}\n"
                        f"Open tabs: {self.ch.tab_summary()}\n"
                        f"Clipboard: {self.ch.clipboard.summary() or '(empty)'}\n"
                        f"Last result: {feedback[:800] or '(nothing yet)'}\n"
                        f"Page right now:\n{serialize.render_compact(snap)}"
                    )
                    plan = await self.orchestrator.replan(task, step=n, situation=situation)
                    self._emit(
                        "plan",
                        step=n,
                        stage=plan.stage,
                        stages=plan.stages,
                        current=plan.current,
                        assessment=plan.assessment,
                    )
                    if plan.done:
                        # The planner says every stage is finished. Let the step
                        # model write the answer rather than inventing one here —
                        # it is the one that saw the pages.
                        feedback = (
                            f"{feedback}\n\nThe planner considers every stage complete: "
                            f"{plan.assessment} Call `done` now with the full answer, "
                            f"using these gathered notes:\n{plan.notes}"
                        ).strip()
                plan_block = self.orchestrator.plan.render()
                await self.ch.overlay.think(goal=self.orchestrator.plan.stage or task)

            # An embedder's annotation goes in with the step's feedback, so the
            # model reads it before the page rather than after. Guarded: a caller's
            # bug must not end a run that is otherwise fine.
            if self.annotate is not None:
                try:
                    extra = await self.annotate(self.ch, snap)
                except Exception:
                    log.debug("annotator raised", exc_info=True)
                    extra = ""
                if extra:
                    feedback = f"{feedback}\n\n{extra}" if feedback else extra

            observation = prompt.observation(
                snap, step=n, max_steps=cfg.max_steps, feedback=feedback, plan=plan_block
            )
            history.append(Message("user", observation))
            self._compact(history)

            # --- decide ----------------------------------------------------
            try:
                parsed = await self._decide(history)
            except LLMError as exc:
                log.error("model call failed: %s", exc)
                stopped_because = f"the model could not be reached: {exc}"
                summary = "The run stopped because the model was unreachable."
                break

            step.thought = parsed.thought
            step.repairs = parsed.repairs
            if parsed.thought:
                await self.ch.overlay.think(thought=parsed.thought)
            self._emit("thought", step=n, text=parsed.thought, repairs=parsed.repairs)

            if not parsed.ok:
                # Already retried inside _decide; treat as a failed step.
                consecutive_errors += 1
                feedback = prompt.repair_prompt(parsed.error, parsed.repairs, tools=bool(self._tools))
                history.append(Message("assistant", parsed.raw[:1500] or "(unreadable)"))
                steps.append(step)
                if consecutive_errors >= cfg.max_consecutive_errors:
                    stopped_because = "the model kept producing unreadable output"
                    summary = "Could not get a valid action from the model."
                    break
                continue

            history.append(Message("assistant", _echo(parsed)))

            # --- act -------------------------------------------------------
            results: list[ActionResult] = []
            finished: Done | None = None

            for seq, action in enumerate(parsed.actions):
                step.actions.append(action.action)
                if action.why:
                    await self.ch.overlay.think(thought=f"{parsed.thought} — {action.why}".strip(" —"))
                result = await self.ch.act(action)
                results.append(result)
                self._emit(
                    "action", step=n, name=action.action, ok=result.ok, detail=result.message
                )
                if self.store:
                    self.store.record_action(
                        n,
                        seq,
                        name=action.action,
                        args=action.model_dump(exclude_defaults=True, exclude={"action", "why"}),
                        why=action.why,
                        outcome=str(result.outcome),
                        message=result.message,
                        repairs=result.repairs,
                        ms=result.duration_ms,
                    )

                if isinstance(action, Done) and result.ok:
                    finished = action
                    break
                if not result.ok:
                    # Stop the batch: the page state the later actions assumed is
                    # no longer trustworthy.
                    break

            step.results = results
            step.ms = int((time.monotonic() - step_started) * 1000)
            steps.append(step)
            if self.store:
                self.store.record_step(
                    n,
                    thought=step.thought,
                    url=step.url,
                    title=snap.title,
                    fingerprint=step.fingerprint,
                    ms=step.ms,
                )

            if finished is not None:
                success = finished.success
                summary = finished.summary
                stopped_because = "the agent finished"
                break

            feedback = format_batch(results)
            if parsed.repairs:
                feedback += (
                    "\n\nNote on format: "
                    + "; ".join(parsed.repairs)
                    + ". I understood you, but match the format exactly next time."
                )

            if all(r.ok for r in results):
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= cfg.max_consecutive_errors:
                    stopped_because = (
                        f"{consecutive_errors} steps failed in a row"
                    )
                    summary = (
                        "Stopped after repeated failures. Last error: "
                        + next((r.message for r in reversed(results) if not r.ok), "unknown")
                    )
                    break

        elapsed = time.monotonic() - started
        if not summary:
            summary = (
                f"Ran out of steps after {len(steps)} attempts without reaching a "
                "conclusion."
            )

        await self.ch.overlay.think(
            thought=summary[:400],
            status="ok" if success else "warn",
            action="finished",
            step=f"{len(steps)} steps",
        )
        decisions = self.via_tools + self.via_text
        self._emit(
            "run_end",
            success=success,
            summary=summary,
            steps=len(steps),
            via_tools=self.via_tools,
            via_text=self.via_text,
            tool_rate=(self.via_tools / decisions) if decisions else 0.0,
        )

        if self.store:
            self.store.end_run(
                success=success,
                summary=summary,
                stopped_because=stopped_because,
                input_tokens=self.llm.total_input,
                output_tokens=self.llm.total_output,
            )
            # Any URL the summary cites is a source that fed the answer, as
            # distinct from one that was merely opened and rejected.
            cited = re.findall(r"https?://[^\s)\]\"'>]+", summary)
            if cited:
                self.store.mark_used(cited)

        return RunResult(
            task=task,
            success=success,
            summary=summary,
            steps=steps,
            elapsed_s=elapsed,
            input_tokens=self.llm.total_input,
            output_tokens=self.llm.total_output,
            stopped_because=stopped_because,
        )

    # ----------------------------------------------------------------- decide

    async def _decide(self, history: list[Message], *, attempts: int = 3) -> parse.Parsed:
        """Ask the model, and give it a second chance at the format if needed.

        The retry happens here rather than costing a loop step, because a malformed
        reply is not a wrong decision — the model usually knows what it wants and
        just said it badly. Repeated failures still fall through to the loop, which
        counts them as errors and eventually stops.
        """
        working = list(history)
        last = parse.Parsed(error="no attempt made")

        for attempt in range(attempts):
            response = await self.llm.complete(self.system, working, tools=self._tools)

            if response.tool_calls:
                self.via_tools += 1
                last = parse.parse_tool_calls(response.tool_calls, response.text)
            elif response.empty:
                self.via_text += 1
                last = parse.Parsed(error="Your reply was empty.")
            else:
                # A text reply while tools were offered is the interesting case:
                # the model *chose* prose. Counted separately so the tool-calling
                # rate is a number rather than an inference from error messages.
                self.via_text += 1
                last = parse.parse_response(response.text)

            if last.ok:
                if last.repairs and attempt == 0:
                    log.debug("format repaired: %s", last.repairs)
                return last

            log.info("unreadable model reply (attempt %d): %s", attempt + 1, last.error)
            # The raw text is the only thing that makes "no recognisable action"
            # diagnosable after the fact — without it you know the reply was bad
            # but not in which way, and cannot improve the repair rules.
            log.debug("raw reply was: %r", (response.text or "")[:1200])
            self._emit("parse_retry", attempt=attempt + 1, error=last.error)
            working = [
                *working,
                Message("assistant", (response.text or "(empty)")[:1500]),
                Message(
                    "user",
                    prompt.repair_prompt(last.error, tools=bool(self._tools)),
                ),
            ]
            await asyncio.sleep(0.2)

        return last

    # ---------------------------------------------------------------- history

    @staticmethod
    def _compact(history: list[Message]) -> None:
        """Collapse old observations in place.

        Only user-role observation messages are compacted; the task and the
        assistant's own decisions stay, because the chain of what it chose and why
        is what keeps a long run coherent.
        """
        observations = [
            i
            for i, m in enumerate(history)
            if m.role == "user" and m.content.startswith("# Step ")
        ]
        for index in observations[:-_FULL_HISTORY]:
            message = history[index]
            if message.content.startswith("# Step ") and "\n" in message.content:
                header = message.content.split("\n", 1)[0]
                if "(page state omitted" in message.content:
                    continue
                url = ""
                for line in message.content.splitlines():
                    if line.startswith("http"):
                        url = line
                        break
                history[index] = Message(
                    "user",
                    f"{header} — {url}\n(page state omitted; only the last "
                    f"{_FULL_HISTORY} observations are kept in full)",
                )


def _echo(parsed: parse.Parsed) -> str:
    """The assistant turn recorded in history.

    The canonical form is recorded rather than the model's raw text, so a model
    that got the format wrong sees its own history in the *right* shape. Echoing
    the mistake back teaches the mistake.
    """
    import json

    return json.dumps(
        {
            "thought": parsed.thought,
            "actions": [a.model_dump(exclude_defaults=True) for a in parsed.actions],
        },
        ensure_ascii=False,
    )[:2000]


async def run_task(
    chamber: Chamber,
    task: str,
    *,
    start_url: str | None = None,
    on_event: Listener | None = None,
    system_extra: str = "",
    annotate: Annotator | None = None,
) -> RunResult:
    """Convenience entry point: build an LLM and a trace store from config, run once."""
    import contextlib
    from contextlib import AsyncExitStack

    from chamber.trace.store import open_store

    cfg = chamber.config
    with contextlib.ExitStack() as stack:
        store = stack.enter_context(open_store()) if cfg.trace else None
        async with AsyncExitStack() as aes:
            # Roles let a viewer tell the three models apart in one event stream.
            llm = await aes.enter_async_context(
                LLM(cfg.model, observer=on_event, role="step")
            )
            if chamber.vision is not None:
                chamber.vision.attach_observer(on_event)
            orchestrator = None
            if cfg.orchestrator is not None:
                planner = await aes.enter_async_context(
                    LLM(cfg.orchestrator, observer=on_event, role="planner")
                )
                orchestrator = Orchestrator(planner)
            agent = Agent(
                chamber,
                llm,
                on_event=on_event,
                store=store,
                orchestrator=orchestrator,
                system_extra=system_extra,
                annotate=annotate,
            )
            return await agent.run(task, start_url=start_url)
