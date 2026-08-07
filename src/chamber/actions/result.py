"""Action outcomes, and the feedback the model reads when one fails.

This module is small and matters a lot. An agent loop is a conversation, and the
error string is half of it. `"Error: Timeout 30000ms exceeded"` tells a model
nothing it can act on, so it retries the identical action and the loop stalls until
the step budget runs out.

So every failure mode carries a **hint**: a specific, different thing to try. The
rule for writing one is that it must name an action the model can actually take —
"scroll down to bring it into view", "the button is covered by a cookie banner,
dismiss that first" — never "please try again".

Failures are also classified as retryable or not. The executor retries the
retryable ones itself, silently, so the model never spends a step on a race it
could not have known about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    OK = "ok"

    # The model's fault — it gets these back and should choose differently.
    INVALID_ACTION = "invalid_action"
    UNKNOWN_REF = "unknown_ref"
    WRONG_ELEMENT = "wrong_element"

    # The page's fault — recoverable, often by the executor alone.
    STALE_REF = "stale_ref"
    OCCLUDED = "occluded"
    DISABLED = "disabled"
    NOT_INTERACTABLE = "not_interactable"
    TIMEOUT = "timeout"
    NAVIGATION_FAILED = "navigation_failed"

    # Needs a human.
    CHALLENGE = "challenge"

    # Everything else.
    JS_ERROR = "js_error"
    BROWSER_ERROR = "browser_error"


_HINTS: dict[Outcome, str] = {
    Outcome.INVALID_ACTION: (
        "Re-read the action format above and emit exactly that shape. Every action "
        "needs an \"action\" field naming it."
    ),
    Outcome.UNKNOWN_REF: (
        "That ref is not in the current control list. Refs are reassigned on every "
        "observation — use one from the list you were just shown, never one you "
        "remember from an earlier step."
    ),
    Outcome.WRONG_ELEMENT: (
        "Pick an element whose role matches the action: type_text needs a textbox, "
        "select_option needs a combobox or listbox."
    ),
    Outcome.STALE_REF: (
        "The page re-rendered and that element no longer exists. Look at the fresh "
        "control list below and pick the equivalent element."
    ),
    Outcome.OCCLUDED: (
        "Something is painted on top of the target. Dismiss it first — cookie "
        "banners, modals and newsletter popups all have a close or accept control "
        "in the list."
    ),
    Outcome.DISABLED: (
        "The control is disabled. Something else has to happen first — usually a "
        "required field is empty or a checkbox is unticked."
    ),
    Outcome.NOT_INTERACTABLE: (
        "The element is present but not accepting input. It may be animating in, or "
        "behind an inert overlay. Try wait_for, then look again."
    ),
    Outcome.TIMEOUT: (
        "The page did not settle in time. If it is a heavy app, wait_for a specific "
        "element rather than waiting on the whole page."
    ),
    Outcome.NAVIGATION_FAILED: (
        "The navigation did not complete. Check the URL, or the site may be "
        "refusing the request."
    ),
    Outcome.CHALLENGE: (
        "A bot check is blocking the page. Use ask_human with reason='captcha' — do "
        "not attempt to solve it."
    ),
    Outcome.JS_ERROR: "The expression threw. Check it returns JSON-safe data.",
    Outcome.BROWSER_ERROR: "The browser rejected the operation. Try a different approach.",
}


@dataclass(slots=True)
class ActionResult:
    """What happened, in a form both the trace and the model can consume."""

    outcome: Outcome
    action: str = ""
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    duration_ms: int = 0
    # Set when the executor fixed something on the model's behalf — scrolled an
    # element into view, re-resolved a stale ref. Surfaced so the model learns the
    # page's behaviour instead of being quietly carried.
    repairs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def hint(self) -> str:
        return _HINTS.get(self.outcome, "")

    def for_model(self) -> str:
        """The single string handed back to the model after this action."""
        if self.ok:
            line = f"✓ {self.action}"
            if self.message:
                line += f" — {self.message}"
            if self.repairs:
                line += f"\n  (chamber adjusted: {'; '.join(self.repairs)})"
            return line

        parts = [f"✗ {self.action} failed [{self.outcome}]"]
        if self.message:
            parts.append(f"  {self.message}")
        hint = self.hint
        if hint:
            parts.append(f"  → {hint}")
        return "\n".join(parts)

    # --- constructors -------------------------------------------------------

    @classmethod
    def success(
        cls, action: str, message: str = "", /, **data: Any
    ) -> ActionResult:
        return cls(Outcome.OK, action=action, message=message, data=data)

    @classmethod
    def failure(
        cls,
        outcome: Outcome,
        action: str,
        message: str = "",
        /,
        *,
        retryable: bool = False,
        **data: Any,
    ) -> ActionResult:
        return cls(
            outcome, action=action, message=message, retryable=retryable, data=data
        )


def format_batch(results: list[ActionResult]) -> str:
    """Feedback for a multi-action step.

    Numbered because the model needs to know *which* of its actions failed when it
    sent three — an unlabelled list of outcomes is guesswork.
    """
    if not results:
        return "(no actions were executed)"
    if len(results) == 1:
        return results[0].for_model()
    lines = []
    for i, r in enumerate(results, 1):
        body = r.for_model().replace("\n", "\n   ")
        lines.append(f"{i}. {body}")
    if any(not r.ok for r in results):
        first_bad = next(i for i, r in enumerate(results, 1) if not r.ok)
        if first_bad < len(results):
            lines.append(
                f"\nActions after #{first_bad} were skipped — the page state changed."
            )
    return "\n".join(lines)
