"""The system prompt, and how a step is presented.

Written with the constraint that a small, cheap model has to follow it. That means:
concrete over abstract, examples over descriptions, and the exact output shape
stated once and demonstrated twice. The earlier prototype's finding was that this
prompt is the difference between a cheap model completing a long task and falling
apart around step ten — so the format section is not boilerplate, it is the feature.

The action reference is *generated* from `actions/schema.py`, not written by hand.
A prompt that drifts from the validator is a machine for producing errors the model
cannot fix, because it is doing exactly what it was told.
"""

from __future__ import annotations

from chamber.actions.schema import ACTION_TYPES
from chamber.dom import serialize
from chamber.dom.model import Snapshot

_PREAMBLE = """\
You are driving a real web browser that a human is watching, right now, on their \
screen. They can see your cursor move, the element you are about to touch, and \
whatever you write in `thought`. Work like someone is looking over your shoulder, \
because someone is.

You see the page as a list of controls, each with a ref like `e12`, plus the \
readable content. You act by naming refs. You do not see pixels unless you ask for \
a screenshot.
"""

_RULES = """\
# How to work

- **One action at a time, usually.** Send several only when none of them can \
change the page for the others — filling two fields of the same form is fine. \
A click followed by anything else is not: the click re-renders the page and every \
ref you were given goes stale.
- **Refs are per-observation.** They are reassigned every step. Never use a ref \
from an earlier step; always take them from the list you were just shown.
- **Read before you act.** The page content is right there. If the answer is \
already on screen, do not click anything — finish.
- **If something is covered, deal with the cover.** Cookie banners, modals and \
newsletter popups all have a close or accept control in the list. Dismiss it, then \
carry on.
- **Scrolling is cheap; guessing is not.** If what you need is not in the list, \
scroll and look again before assuming it does not exist.
- **Copy rather than transcribe.** To carry a product title, a quote, a price or an \
ID to another page, `copy` it and `paste` it. The text goes page → clipboard → page \
without passing through you: it costs no tokens and cannot be mistyped. Re-typing \
something you read is slower and gets details wrong.
- **Use tabs as memory, not your own recall.** Anything you will need again belongs \
in a tab you leave open, not in your head. Comparing two sites? Keep one open in \
each tab and `switch_tab` between them — you will not have to remember either page, \
because you can go back and look. The tab list is in every observation.
- **When a step fails, read the reason.** Every failure says what went wrong and \
suggests a different approach. Repeating an identical failed action never helps.
- **Never solve a captcha and never type credentials.** Use `ask_human`. A person \
is sitting there; handing it over costs them seconds and is always the right move.
- **Finish deliberately.** Call `done` with a summary that actually answers the \
task. If you could not finish, say `done` with `success: false` and explain what \
blocked you — that is a useful result, an exhausted step budget is not.
"""

_FORMAT_TEXT = """\
# Output format

Reply with **one JSON object and nothing else**. No prose before it, no explanation \
after it, no markdown fence.

```
{"thought": "why this, in one or two sentences",
 "actions": [{"action": "<name>", ...fields..., "why": "short reason"}]}
```

Getting this exactly right matters. These are the mistakes that come up most:

    {"click": 42}                                  ✗ not an action object
    {"action": "click", "index": 42}               ✗ the field is "ref", not "index"
    {"action": "click", "ref": 42}                 ✗ refs are strings: "e42"
    {"action": "click", "ref": "e42"}              ✓

    {"action": "type_text", "ref": "e7", "value": "hello"}   ✗ the field is "text"
    {"action": "type_text", "ref": "e7", "text": "hello"}    ✓

Two worked examples:

    {"thought": "The search box is empty; I'll search for the product first.",
     "actions": [{"action": "type_text", "ref": "e5", "text": "wireless mouse",
                  "submit": true, "why": "run the search"}]}

    {"thought": "The results are on screen and the cheapest is the third one. \
That answers the question, so I'm done.",
     "actions": [{"action": "done", "success": true,
                  "summary": "Cheapest wireless mouse listed is the Logitech M185 at £12.99."}]}
"""

_FORMAT_TOOLS = """\
# Output format

**Every step must make a tool call.** Prose alone does nothing — the browser only \
moves when a tool is called. Call exactly one per step, unless two are genuinely \
independent. Put your reasoning in the message text alongside the call; the human \
watching sees it in the browser window.

If you truly cannot call a tool, fall back to a single JSON object and nothing \
else — same names and fields as the tools:

    {"thought": "why this, briefly",
     "actions": [{"action": "click", "ref": "e12", "why": "open the listing"}]}

    {"thought": "the answer is on screen",
     "actions": [{"action": "done", "success": true, "summary": "..."}]}

Never reply with prose and no tool call. That wastes the step entirely.
"""


def _action_reference() -> str:
    """The action list, generated from the schema so it cannot drift."""
    lines = ["# Actions"]
    groups: dict[str, list[str]] = {
        "Move around": ["navigate", "go_back", "go_forward", "reload", "scroll", "scroll_to"],
        "Interact": ["click", "type_text", "press_key", "select_option", "hover", "drag",
                     "upload_file"],
        "Move text between pages": ["copy", "paste", "clipboard"],
        "Tabs": ["open_tab", "switch_tab", "close_tab"],
        "Look closer": ["read_page", "screenshot", "inspect", "console_log", "network_log", "wait_for"],
        "Get past what is in the way": ["dismiss_overlay"],
        "Escape hatches": ["evaluate_js"],
        "Stop": ["ask_human", "done"],
    }

    # Anything not in a group would be silently invisible to the model even though
    # the validator accepts it — a failure mode with no symptom except the model
    # never using a tool it has.
    missing = set(ACTION_TYPES) - {n for names in groups.values() for n in names}
    if missing:  # pragma: no cover - guarded by a test
        groups["Other"] = sorted(missing)

    for heading, names in groups.items():
        lines.append(f"\n**{heading}**")
        for name in names:
            cls = ACTION_TYPES.get(name)
            if cls is None:
                continue
            fields = []
            for fname, field in cls.model_fields.items():
                if fname in ("action", "why"):
                    continue
                required = field.is_required()
                annotation = _type_name(field.annotation)
                fields.append(f"{fname}: {annotation}" + ("" if required else "?"))
            signature = ", ".join(fields)
            doc = (cls.__doc__ or "").strip().split("\n")[0]
            summary = f" — {doc}" if doc and not doc.startswith(name) else ""
            lines.append(f"  `{name}`({signature}){summary}")
    return "\n".join(lines)


def _type_name(annotation: object) -> str:
    text = str(annotation)
    for prefix in ("typing.", "<class '", "'>"):
        text = text.replace(prefix, "")
    text = text.replace("Literal", "one of").replace("NoneType", "null")
    return text[:48]


def system_prompt(*, tool_calling: bool = False, extra: str = "") -> str:
    """Build the system prompt.

    With tool calling on, the generated action reference is **left out**: the tool
    schemas carry every name, field and description already, and repeating ~2,000
    characters of it in the prompt is pure duplication a small model has to hold in
    working memory alongside a 10,000-character page observation. Dropping it takes
    the prompt from ~5,800 characters to ~3,800.

    With tool calling off, the reference is the only description of the vocabulary
    the model has, so it stays.
    """
    parts = [_PREAMBLE]
    if not tool_calling:
        parts.append(_action_reference())
    parts.append(_RULES)
    parts.append(_FORMAT_TOOLS if tool_calling else _FORMAT_TEXT)
    if extra:
        parts.append(extra)
    return "\n\n".join(p.strip() for p in parts)


def task_prompt(task: str) -> str:
    return f"# Your task\n\n{task}\n\nStart by looking at what is on screen."


def observation(
    snap: Snapshot,
    *,
    step: int,
    max_steps: int,
    feedback: str = "",
    plan: str = "",
    include_content: bool = True,
) -> str:
    """One turn of input: the plan, what happened, then what the page looks like."""
    header = f"# Step {step} of {max_steps}"
    parts = [header]

    # The plan goes first: it is what the step is *for*, and the model should read
    # it before it reads the page.
    if plan:
        parts.append(plan)

    if feedback:
        parts.append("## Result of your last step\n" + feedback)

    parts.append(serialize.render(snap, include_content=include_content))

    if step >= max_steps - 3:
        parts.append(
            "_You are near the step limit. If you have enough to answer, call `done` now._"
        )
    return "\n\n".join(parts)


def repair_prompt(error: str, repairs: list[str] | None = None, *, tools: bool = False) -> str:
    """Sent when the reply could not be read as an action.

    This carries the *full* format specification, not a one-line reminder. It is
    sent rarely — only after a reply failed — so the tokens are cheap here in a way
    they are not in the system prompt, and this is precisely the moment the model
    has demonstrated it needs them.

    Learned the hard way: shortening the tool-mode system prompt was right for the
    happy path, but a model that replies in prose despite having tools then had
    *less* guidance than before, and the repair rate went up rather than down.
    """
    lines = [f"That reply did not contain an action I could execute.\n\n{error}"]
    if tools:
        lines.append(
            "You have tools available — call one. A reply with no tool call does "
            "nothing at all."
        )
    if repairs:
        lines.append("Things I had to fix last time: " + "; ".join(repairs))
    lines.append(_FORMAT_TEXT)
    return "\n\n".join(lines)
