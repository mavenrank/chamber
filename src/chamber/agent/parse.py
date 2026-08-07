"""Getting a valid action out of whatever the model actually said.

The earlier prototype's clearest finding was that cheap models know *what* to do
long before they can reliably say it in the required shape. They wrap JSON in
prose, fence it, use the action name as the key instead of a field, or pass a bare
number where an object was specified. Every one of those is a formatting slip, not
a reasoning failure — and rejecting them costs a full round trip to fix something
the model never got wrong.

So this normalises the near-misses and rejects only what is genuinely ambiguous.
The design rule: **repair syntax, never guess intent.** Rewriting `{"click": 42}`
into `{"action": "click", "ref": "e42"}` is safe, because there is exactly one
reading. Picking an element because the model named a button that does not exist is
not, and that goes back as feedback instead.

Every repair is recorded in `Parsed.repairs` and surfaced to the model, so it
converges on the right format rather than being carried indefinitely.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from chamber.actions.schema import ACTION_TYPES, ActionEnvelope, AnyAction, parse_action

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json|javascript|js)?\s*\n(.*?)```", re.S | re.I)

# Names a model reaches for that are not the canonical ones. Kept short — this is
# for genuine synonyms, not for guessing.
_ALIASES: dict[str, str] = {
    "goto": "navigate",
    "go_to": "navigate",
    "open": "navigate",
    "open_url": "navigate",
    "visit": "navigate",
    "input": "type_text",
    "type": "type_text",
    "fill": "type_text",
    "enter_text": "type_text",
    "write": "type_text",
    "press": "press_key",
    "key": "press_key",
    "keyboard": "press_key",
    "select": "select_option",
    "choose": "select_option",
    "back": "go_back",
    "forward": "go_forward",
    "refresh": "reload",
    "extract": "read_page",
    "read": "read_page",
    "get_text": "read_page",
    "finish": "done",
    "complete": "done",
    "answer": "done",
    "ask": "ask_human",
    "help": "ask_human",
    "human": "ask_human",
    "eval": "evaluate_js",
    "execute_js": "evaluate_js",
    "console": "console_log",
    "network": "network_log",
    "wait": "wait_for",
    "sleep": "wait_for",
}

# Field names a model substitutes for `ref`.
_REF_ALIASES = ("ref", "index", "element", "element_id", "id", "target", "selector_ref")


@dataclass(slots=True)
class Parsed:
    envelope: ActionEnvelope | None = None
    error: str = ""
    repairs: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.envelope is not None

    @property
    def actions(self) -> list[AnyAction]:
        return list(self.envelope.actions) if self.envelope else []

    @property
    def thought(self) -> str:
        return self.envelope.thought if self.envelope else ""


# ------------------------------------------------------------------ extraction


def _json_blobs(text: str) -> list[str]:
    """Every plausible JSON object in the text, best candidate first."""
    out: list[str] = []

    for match in _FENCE.finditer(text):
        out.append(match.group(1).strip())

    # Brace matching rather than a regex: nested objects are the normal case here
    # and a regex cannot balance them.
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : i + 1])
                start = -1
            elif depth < 0:
                depth = 0

    # Longest first: a model that emits both a summary object and the real action
    # usually makes the real one bigger.
    seen: set[str] = set()
    ordered = sorted(out, key=len, reverse=True)
    return [b for b in ordered if not (b in seen or seen.add(b))]


def _load(blob: str) -> Any | None:
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    # Trailing commas and single quotes are the two survivable syntax slips.
    patched = re.sub(r",(\s*[}\]])", r"\1", blob)
    try:
        return json.loads(patched)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- normalisation


def _normalise_ref(value: Any) -> str | None:
    """Coerce whatever the model called an element into a ref string.

    A bare integer is the classic case: browser-use style indexes it as `42`, and
    chamber's refs are `e42`. That mapping is unambiguous, so it is safe.
    """
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if re.fullmatch(r"e\d+", v):
            return v
        if v.isdigit():
            return "e" + v
        return v
    if isinstance(value, int) and not isinstance(value, bool):
        return f"e{value}"
    return None


def _normalise_action(obj: Any, repairs: list[str]) -> dict[str, Any] | None:
    """One action dict, in canonical form, or None if it cannot be read."""
    if not isinstance(obj, dict):
        return None

    data = dict(obj)

    # Form A: {"action": "click", "ref": "e12"} — already canonical.
    name = data.pop("action", None) or data.pop("name", None) or data.pop("tool", None)

    # Form B: {"click": {"ref": "e12"}} or {"click": 42} — the action name is the
    # only key. This is the shape cheap models fall into most often.
    if name is None:
        candidates = [k for k in data if k.lower() in ACTION_TYPES or k.lower() in _ALIASES]
        if len(candidates) == 1:
            name = candidates[0]
            payload = data.pop(name)
            if isinstance(payload, dict):
                data = {**payload, **data}
            elif isinstance(payload, (str, int)) and not isinstance(payload, bool):
                # {"click": 42} / {"navigate": "https://…"}
                canonical = _ALIASES.get(str(name).lower(), str(name).lower())
                if canonical in ("navigate", "open_tab"):
                    data = {"url": str(payload), **data}
                elif canonical == "press_key":
                    data = {"key": str(payload), **data}
                elif canonical == "done":
                    data = {"summary": str(payload), **data}
                else:
                    ref = _normalise_ref(payload)
                    if ref:
                        data = {"ref": ref, **data}
                repairs.append(
                    f'"{name}" was given a bare value; actions take an object of named fields'
                )

    if name is None:
        return None

    # Function-calling style, where the fields live in a nested object:
    #   {"action": "click", "parameters": {"ref": "e12"}}
    # Models trained heavily on tool-calling reach for this even when asked for a
    # flat shape, and it carries exactly one reading, so flatten it.
    for wrapper in ("parameters", "params", "args", "arguments", "input", "payload"):
        nested_args = data.get(wrapper)
        if isinstance(nested_args, dict):
            data.pop(wrapper)
            data = {**nested_args, **data}
            repairs.append(f'unwrapped "{wrapper}" — action fields go at the top level')
            break

    key = str(name).strip().lower()
    canonical = _ALIASES.get(key, key)
    if canonical != key:
        repairs.append(f'"{key}" is not an action name; read as "{canonical}"')
    if canonical not in ACTION_TYPES:
        return None

    # Ref field aliases.
    if "ref" not in data:
        for alias in _REF_ALIASES:
            if alias in data:
                ref = _normalise_ref(data.pop(alias))
                if ref:
                    data["ref"] = ref
                    if alias != "ref":
                        repairs.append(f'"{alias}" read as "ref"')
                break
    elif (ref := _normalise_ref(data["ref"])) is not None and ref != data["ref"]:
        repairs.append(f"ref {data['ref']!r} normalised to {ref!r}")
        data["ref"] = ref

    # Common field-name slips, per action.
    if canonical == "type_text":
        for alias in ("value", "content", "input", "query"):
            if alias in data and "text" not in data:
                data["text"] = data.pop(alias)
                repairs.append(f'"{alias}" read as "text"')
    if canonical == "done":
        for alias in ("text", "result", "answer", "output", "message"):
            if alias in data and "summary" not in data:
                data["summary"] = data.pop(alias)
                repairs.append(f'"{alias}" read as "summary"')
    if canonical == "ask_human":
        for alias in ("text", "message", "prompt"):
            if alias in data and "question" not in data:
                data["question"] = data.pop(alias)
    if canonical == "navigate":
        for alias in ("href", "link", "address"):
            if alias in data and "url" not in data:
                data["url"] = data.pop(alias)

    # Drop fields the schema forbids rather than failing the whole action — an
    # extra "confidence": 0.9 should not cost a step.
    fields = ACTION_TYPES[canonical].model_fields
    allowed = set(fields)
    extra = [k for k in data if k not in allowed]
    for k in extra:
        data.pop(k)
    if extra:
        repairs.append(f"ignored unknown field(s): {', '.join(extra)}")

    # Clamp numbers to their bounds instead of rejecting them. Observed in a real
    # run: the model asked for `read_page` with a budget below the 1000 minimum and
    # lost a full round trip to `Input should be greater than or equal to 1000`.
    # The bound is chamber's implementation detail; the intent — "read less" — has
    # exactly one reading, so honouring it as closely as the schema permits is
    # strictly better than refusing.
    for name, value in list(data.items()):
        field = fields.get(name)
        if field is None:
            continue

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            low, high = _bounds(field)
            if low is not None and value < low:
                data[name] = low
                repairs.append(f"{name}={value} raised to the minimum {low}")
            elif high is not None and value > high:
                data[name] = high
                repairs.append(f"{name}={value} lowered to the maximum {high}")
            continue

        # An out-of-set enum value falls back to the field's default rather than
        # failing the action. Observed on `ask_human`, where the model invented a
        # `reason` outside the allowed set — and rejecting the escape hatch is the
        # worst possible moment to be strict, because the model reaches for it
        # precisely when it is already stuck.
        if isinstance(value, str):
            allowed_values = _literal_values(field)
            if allowed_values and value not in allowed_values:
                fallback = field.get_default(call_default_factory=False)
                if fallback in allowed_values:
                    data[name] = fallback
                    repairs.append(
                        f"{name}={value!r} is not one of {sorted(allowed_values)}; used {fallback!r}"
                    )

    data["action"] = canonical
    return data


def _literal_values(field: Any) -> set[str]:
    """The allowed values of a `Literal[...]` field, or an empty set."""
    from typing import Literal, get_args, get_origin

    annotation = getattr(field, "annotation", None)
    if get_origin(annotation) is Literal:
        return {v for v in get_args(annotation) if isinstance(v, str)}
    return set()


def _bounds(field: Any) -> tuple[float | None, float | None]:
    """Read `ge`/`le` (and `gt`/`lt`) off a pydantic field's metadata."""
    low = high = None
    for constraint in getattr(field, "metadata", ()) or ():
        for attr, is_low in (("ge", True), ("gt", True), ("le", False), ("lt", False)):
            value = getattr(constraint, attr, None)
            if value is None:
                continue
            if is_low:
                low = value if low is None else max(low, value)
            else:
                high = value if high is None else min(high, value)
    return low, high


def _envelope_from(obj: Any, repairs: list[str]) -> dict[str, Any] | None:
    """Coerce whatever came back into {thought, actions:[...]}"""
    if isinstance(obj, list):
        actions = [a for a in (_normalise_action(x, repairs) for x in obj) if a]
        return {"thought": "", "actions": actions} if actions else None

    if not isinstance(obj, dict):
        return None

    thought = ""
    for key in ("thought", "reasoning", "thinking", "rationale", "plan", "observation"):
        if isinstance(obj.get(key), str):
            thought = obj[key]
            break

    raw_actions = obj.get("actions") or obj.get("action_list") or obj.get("steps")
    if isinstance(raw_actions, list):
        actions = [a for a in (_normalise_action(x, repairs) for x in raw_actions) if a]
        if actions:
            return {"thought": thought, "actions": actions}

    # A single action at the top level, possibly alongside a thought field.
    single = _normalise_action(obj, repairs)
    if single:
        if "actions" not in obj and thought:
            repairs.append("wrapped a single top-level action into actions[]")
        return {"thought": thought, "actions": [single]}

    # {"thought": "...", "action": {...}}
    nested = obj.get("action") or obj.get("tool_call")
    if isinstance(nested, dict):
        one = _normalise_action(nested, repairs)
        if one:
            return {"thought": thought, "actions": [one]}

    return None


# --------------------------------------------------------------------- public


def parse_response(text: str) -> Parsed:
    """Parse a text-mode model response into a validated envelope."""
    repairs: list[str] = []
    blobs = _json_blobs(text)

    if not blobs:
        return Parsed(
            error=(
                "No JSON object found in your reply. Respond with a JSON object "
                'like {"thought": "...", "actions": [{"action": "click", "ref": "e3"}]} '
                "and nothing else."
            ),
            raw=text,
        )

    last_error = ""
    for blob in blobs:
        loaded = _load(blob)
        if loaded is None:
            last_error = "The JSON in your reply is malformed."
            continue
        candidate = _envelope_from(loaded, repairs)
        if candidate is None:
            last_error = (
                "That JSON has no recognisable action. Every action needs an "
                '"action" field naming one of: ' + ", ".join(sorted(ACTION_TYPES))
            )
            continue
        try:
            envelope = ActionEnvelope.model_validate(candidate)
        except Exception as exc:
            last_error = _explain(exc)
            continue
        return Parsed(envelope=envelope, repairs=repairs, raw=text)

    return Parsed(error=last_error or "Could not read an action from your reply.", raw=text)


def parse_tool_calls(calls: list[Any], thought: str = "") -> Parsed:
    """Build an envelope from native tool calls."""
    repairs: list[str] = []
    actions: list[dict[str, Any]] = []
    for call in calls:
        payload = dict(getattr(call, "arguments", {}) or {})
        payload["action"] = getattr(call, "name", "")
        normalised = _normalise_action(payload, repairs)
        if normalised:
            actions.append(normalised)

    if not actions:
        return Parsed(error="The tool calls did not name a known action.")
    try:
        envelope = ActionEnvelope.model_validate({"thought": thought, "actions": actions})
    except Exception as exc:
        return Parsed(error=_explain(exc))
    return Parsed(envelope=envelope, repairs=repairs)


def parse_one(payload: dict[str, Any]) -> AnyAction:
    """Normalise then validate a single action dict. Raises on failure."""
    repairs: list[str] = []
    normalised = _normalise_action(payload, repairs)
    if normalised is None:
        raise ValueError(f"Not a recognisable action: {json.dumps(payload)[:200]}")
    return parse_action(normalised)


def _explain(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)[:300]
    return "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
        for e in errors()[:5]
    )
