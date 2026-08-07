"""The action vocabulary — the only thing a model is allowed to say.

Every action is a pydantic model in one discriminated union, which buys three
things at once:

* **A validation boundary.** Nothing reaches the browser until it has the right
  shape. A cheap model that emits `{"click": 42}` gets a typed error back with the
  correct form, not an exception halfway through a click.
* **A generated schema.** `tool_schemas()` derives function-calling definitions
  straight from these classes, so the prompt and the executor cannot drift apart —
  there is exactly one place where an action is defined.
* **Readable failures.** Field descriptions are written for the model, because they
  are what it reads when it gets something wrong.

Naming follows what the model would say, not what Playwright calls it: `type_text`
rather than `fill`, `go_back` rather than `goBack`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Free-text reason shown in the HUD and the trace. Optional so a terse model is
    # not blocked, but the prompt asks for it — it is the "shows its thinking" part
    # and costs almost nothing.
    why: str = Field("", description="One short sentence: why this action, right now.")


# --------------------------------------------------------------- navigation


class Navigate(_Base):
    action: Literal["navigate"] = "navigate"
    url: str = Field(description="Absolute URL, including https://")

    @field_validator("url")
    @classmethod
    def _absolute(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        if v.startswith(("http://", "https://", "file://", "about:")):
            return v
        if "://" in v:
            raise ValueError(f"unsupported scheme in {v!r}")
        # A bare domain is the single most common near-miss; fixing it silently is
        # better than burning a step on a correction round trip.
        return "https://" + v


class GoBack(_Base):
    action: Literal["go_back"] = "go_back"


class GoForward(_Base):
    action: Literal["go_forward"] = "go_forward"


class Reload(_Base):
    action: Literal["reload"] = "reload"


# ------------------------------------------------------------------ pointer


class Click(_Base):
    action: Literal["click"] = "click"
    ref: str = Field(description="Element ref from the control list, e.g. 'e12'")
    button: Literal["left", "right", "middle"] = "left"
    clicks: int = Field(1, ge=1, le=3, description="2 for double-click")
    modifiers: list[Literal["Alt", "Control", "Meta", "Shift"]] = Field(default_factory=list)


class Hover(_Base):
    action: Literal["hover"] = "hover"
    ref: str


class Drag(_Base):
    action: Literal["drag"] = "drag"
    from_ref: str
    to_ref: str


# ----------------------------------------------------------------- keyboard


class TypeText(_Base):
    action: Literal["type_text"] = "type_text"
    ref: str
    text: str
    clear: bool = Field(True, description="Clear the field first. Set false to append.")
    submit: bool = Field(False, description="Press Enter afterwards.")


class PressKey(_Base):
    action: Literal["press_key"] = "press_key"
    key: str = Field(
        description="A key name, e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown', 'Control+a'"
    )
    ref: str | None = Field(None, description="Focus this element first, if given.")


class SelectOption(_Base):
    action: Literal["select_option"] = "select_option"
    ref: str
    value: str = Field(description="The visible option text, or its value attribute.")


# ------------------------------------------------------------------ clipboard


class Copy(_Base):
    """Copy an element's text into chamber's clipboard, without reading it yourself.

    Use this instead of transcribing. The text goes straight from the page into a
    buffer and later into another field, so it never passes through your context:
    it costs almost no tokens, and it cannot be mistyped or half-remembered. Exactly
    right for carrying long product titles, quotes, code or IDs between pages.
    """

    action: Literal["copy"] = "copy"
    ref: str
    label: str = Field(
        "", description="Name this clip so you can paste it alone later. Optional."
    )


class Paste(_Base):
    """Paste copied text into a field, verbatim.

    With no `labels`, every clip is pasted in the order it was copied. The text is
    inserted the way a real paste is, so frameworks see the same events they would
    from Ctrl+V.
    """

    action: Literal["paste"] = "paste"
    ref: str
    labels: list[str] = Field(
        default_factory=list, description="Which clips to paste. Empty means all of them."
    )
    separator: str = Field("\n", description="Placed between clips.")
    prefix: str = Field("", description="Your own text before the pasted content.")
    suffix: str = Field("", description="Your own text after it — e.g. the question you are asking.")
    clear: bool = True
    submit: bool = False


class Clipboard(_Base):
    """List what is currently copied, with a short preview of each clip."""

    action: Literal["clipboard"] = "clipboard"
    clear: bool = Field(False, description="Empty the clipboard instead of listing it.")


# ------------------------------------------------------------------- moving


class Scroll(_Base):
    action: Literal["scroll"] = "scroll"
    direction: Literal["up", "down", "top", "bottom"] = "down"
    amount: int = Field(0, description="Pixels. 0 means one viewport height.")
    ref: str | None = Field(
        None, description="Scroll this panel instead of the window (see Scrollable panels)."
    )


class ScrollToRef(_Base):
    action: Literal["scroll_to"] = "scroll_to"
    ref: str


# --------------------------------------------------------------------- tabs


class OpenTab(_Base):
    """Open a second tab and switch to it.

    Use tabs as memory. Comparing two sites is far easier with one open in each
    than with one tab you keep navigating back and forth — you can return and look
    instead of having to remember.
    """

    action: Literal["open_tab"] = "open_tab"
    url: str = ""
    purpose: str = Field(
        "", description="What this tab is for, e.g. 'amazon.in cart'. Shown in the tab list."
    )


class SwitchTab(_Base):
    action: Literal["switch_tab"] = "switch_tab"
    tab_id: str


class CloseTab(_Base):
    action: Literal["close_tab"] = "close_tab"
    tab_id: str | None = Field(None, description="Omit to close the current tab.")


# ------------------------------------------------------------------ waiting


class WaitFor(_Base):
    action: Literal["wait_for"] = "wait_for"
    text: str | None = Field(None, description="Wait until this text appears on the page.")
    selector: str | None = Field(None, description="Wait until this CSS selector matches.")
    ms: int = Field(0, ge=0, le=30_000, description="Or just wait this many milliseconds.")
    timeout_ms: int = Field(15_000, ge=500, le=60_000)


# ---------------------------------------------------------------- inspection


class ReadPage(_Base):
    """Re-read the page with a bigger text budget, optionally the whole layout.

    Exists because the default observation is deliberately compact. When the model
    needs the long tail — a full article, a whole results table — asking for it is
    one cheap step, and far better than inflating every step to cover the worst case.
    """

    action: Literal["read_page"] = "read_page"
    budget: int = Field(16_000, ge=1000, le=60_000)
    whole_page: bool = Field(
        False, description="Skip main-content detection. Use on search results and dashboards."
    )


class Screenshot(_Base):
    """Look at the page as an image, for what the structure cannot tell you.

    Use it for canvas or image-only content, a layout that looks broken, or when
    actions are having no visible effect. A vision model describes what it sees and
    you get the description back in words — so ask a specific question.
    """

    action: Literal["screenshot"] = "screenshot"
    full_page: bool = False
    question: str = Field(
        "",
        description=(
            "What you want to know about the image, e.g. 'is a dialog covering the "
            "Add to Cart button?'. Leave empty for a general description."
        ),
    )


class InspectElement(_Base):
    """Computed styles, box model and listeners for one element — inspect element,
    without the panel."""

    action: Literal["inspect"] = "inspect"
    ref: str


class ConsoleLog(_Base):
    action: Literal["console_log"] = "console_log"
    limit: int = Field(50, ge=1, le=500)
    errors_only: bool = False


class NetworkLog(_Base):
    action: Literal["network_log"] = "network_log"
    limit: int = Field(50, ge=1, le=500)
    url_contains: str | None = None
    failed_only: bool = False


class EvaluateJS(_Base):
    """Run JavaScript in the page and get the result back.

    The escape hatch. Powerful enough to do anything the other actions do, which is
    exactly why it is last in the prompt and described as a fallback: an agent that
    reaches for `evaluate_js` first produces steps nobody can audit, and bypasses
    the cursor, the highlight and the trace that make this system watchable.
    """

    action: Literal["evaluate_js"] = "evaluate_js"
    expression: str = Field(description="A JS expression or arrow function. Must return JSON-safe data.")


# ------------------------------------------------------------ human in the loop


class AskHuman(_Base):
    """Stop and hand the window to the person watching.

    The correct response to a captcha, a login wall, a payment step, or genuine
    ambiguity about what the user wanted. Solving a challenge is not on the menu;
    the handoff is the feature.
    """

    action: Literal["ask_human"] = "ask_human"
    question: str = Field(description="What you need the human to do or decide.")
    reason: Literal["captcha", "login", "payment", "ambiguous", "blocked", "confirm"] = "blocked"
    resume_when: str = Field(
        "", description="How you will know it is done, e.g. 'the results page loads'."
    )


class Done(_Base):
    action: Literal["done"] = "done"
    success: bool = True
    summary: str = Field(description="What you found or did. This is the answer to the task.")


AnyAction = Annotated[
    Navigate
    | GoBack
    | GoForward
    | Reload
    | Click
    | Hover
    | Drag
    | TypeText
    | PressKey
    | SelectOption
    | Copy
    | Paste
    | Clipboard
    | Scroll
    | ScrollToRef
    | OpenTab
    | SwitchTab
    | CloseTab
    | WaitFor
    | ReadPage
    | Screenshot
    | InspectElement
    | ConsoleLog
    | NetworkLog
    | EvaluateJS
    | AskHuman
    | Done,
    Field(discriminator="action"),
]


class ActionEnvelope(BaseModel):
    """What the model returns each step.

    `thought` is separate from each action's `why` on purpose: the thought covers
    the step (what I understand, what I am trying to achieve), the `why` covers the
    individual action. Both land in the HUD; the thought is what a human reads to
    follow along.
    """

    model_config = ConfigDict(extra="forbid")

    thought: str = Field("", description="Your reasoning for this step, one or two sentences.")
    actions: list[AnyAction] = Field(
        min_length=1,
        max_length=5,
        description=(
            "One action, usually. Batch only actions that cannot invalidate each other — "
            "typing into two fields of the same form is fine; clicking then typing is not, "
            "because the click changes the page and your refs go stale."
        ),
    )


ACTION_TYPES: dict[str, type[_Base]] = {
    cls.model_fields["action"].default: cls  # type: ignore[union-attr]
    for cls in (
        Navigate, GoBack, GoForward, Reload, Click, Hover, Drag, TypeText, PressKey,
        SelectOption, Copy, Paste, Clipboard, Scroll, ScrollToRef, OpenTab, SwitchTab,
        CloseTab, WaitFor, ReadPage, Screenshot, InspectElement, ConsoleLog,
        NetworkLog, EvaluateJS, AskHuman, Done,
    )
}

# Actions that need a live element. Used by the executor to decide whether a ref
# must be resolved, and by the repair pass to give a precise error.
REF_FIELDS: dict[str, tuple[str, ...]] = {
    "click": ("ref",),
    "hover": ("ref",),
    "drag": ("from_ref", "to_ref"),
    "type_text": ("ref",),
    "select_option": ("ref",),
    "copy": ("ref",),
    "paste": ("ref",),
    "scroll_to": ("ref",),
    "inspect": ("ref",),
}


def tool_schemas() -> list[dict[str, Any]]:
    """Function-calling definitions, derived from the models themselves.

    One source of truth: adding an action class adds a tool, updates the prompt and
    updates validation together. Nothing to keep in sync by hand.
    """
    out: list[dict[str, Any]] = []
    for name, cls in ACTION_TYPES.items():
        schema = cls.model_json_schema()
        schema.pop("title", None)
        props = schema.get("properties", {})
        props.pop("action", None)
        required = [r for r in schema.get("required", []) if r != "action"]
        out.append(
            {
                "name": name,
                "description": (cls.__doc__ or "").strip().split("\n\n")[0] or name,
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            }
        )
    return out


def parse_action(payload: dict[str, Any]) -> AnyAction:
    """Validate one action dict. Raises `pydantic.ValidationError`."""
    from pydantic import TypeAdapter

    return TypeAdapter(AnyAction).validate_python(payload)


__all__ = [
    "ACTION_TYPES",
    "REF_FIELDS",
    "ActionEnvelope",
    "AnyAction",
    "AskHuman",
    "Click",
    "CloseTab",
    "ConsoleLog",
    "Done",
    "Drag",
    "EvaluateJS",
    "GoBack",
    "GoForward",
    "Hover",
    "InspectElement",
    "Navigate",
    "NetworkLog",
    "OpenTab",
    "PressKey",
    "ReadPage",
    "Reload",
    "Screenshot",
    "Scroll",
    "ScrollToRef",
    "SelectOption",
    "SwitchTab",
    "TypeText",
    "ValidationError",
    "WaitFor",
    "parse_action",
    "tool_schemas",
]
