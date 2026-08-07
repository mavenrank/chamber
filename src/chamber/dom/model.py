"""Typed page state.

These are the objects every other layer talks in. Kept as plain dataclasses with a
`from_js` constructor rather than pydantic models: this data comes out of a script
we wrote, in a shape we control, hundreds of elements at a time, and paying
validation cost on every snapshot buys nothing. Pydantic is used where it earns its
keep — validating what the *model* sends back (`actions/schema.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self


@dataclass(slots=True)
class Element:
    """One thing on the page the agent could act on."""

    ref: str
    tag: str
    role: str
    name: str
    # Viewport coordinates, not document coordinates — the same space Playwright's
    # mouse and the position:fixed overlay use, so no conversion happens anywhere.
    # The cost is that a box is only meaningful until the next scroll, which is why
    # every action re-resolves through `snapshot.resolve()` before executing.
    box: tuple[int, int, int, int]  # x, y, w, h
    point: tuple[int, int]  # where to actually click — off-centre if the centre is covered
    in_viewport: bool = True
    occluded: bool = False
    occluded_by: str | None = None
    path: list[dict[str, str]] = field(default_factory=list)
    frame: str | None = None
    depth: int = 0

    href: str | None = None
    href_abs: str | None = None
    new_tab: bool = False
    input_type: str | None = None
    value: str | None = None
    placeholder: str | None = None
    options: list[str] = field(default_factory=list)
    selected: str | bool | None = None
    checked: bool | None = None
    expanded: bool | None = None
    disabled: bool = False
    required: bool = False
    focused: bool = False
    max_length: int | None = None

    @property
    def is_icon(self) -> bool:
        """Small enough that its label describes an icon, not readable content.

        44px is the standard minimum touch target, so anything at or under it was
        designed as an icon rather than as text a person reads.
        """
        return self.box[2] <= 44 and self.box[3] <= 44

    @property
    def actionable(self) -> bool:
        """Can this be acted on without first fixing something?

        Occluded and off-screen are both recoverable — the executor scrolls or
        dismisses the blocker — so they do not disqualify. Disabled does.
        """
        return not self.disabled

    @classmethod
    def from_js(cls, d: dict[str, Any]) -> Self:
        box = d.get("box") or [0, 0, 0, 0]
        point = d.get("point") or [box[0] + box[2] // 2, box[1] + box[3] // 2]
        return cls(
            ref=d["ref"],
            tag=d.get("tag", ""),
            role=d.get("role", ""),
            name=d.get("name", ""),
            box=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
            point=(int(point[0]), int(point[1])),
            in_viewport=bool(d.get("inViewport", True)),
            occluded=bool(d.get("occluded", False)),
            occluded_by=d.get("occludedBy"),
            path=d.get("path") or [],
            frame=d.get("frame"),
            depth=int(d.get("depth", 0)),
            href=d.get("href"),
            href_abs=d.get("hrefAbs"),
            new_tab=bool(d.get("newTab", False)),
            input_type=d.get("inputType"),
            value=d.get("value"),
            placeholder=d.get("placeholder"),
            options=d.get("options") or [],
            selected=d.get("selected"),
            checked=d.get("checked"),
            expanded=d.get("expanded"),
            disabled=bool(d.get("disabled", False)),
            required=bool(d.get("required", False)),
            focused=bool(d.get("focused", False)),
            max_length=d.get("maxLength"),
        )


@dataclass(slots=True)
class Scrollable:
    """A scroll container that is not the window.

    Reported separately because "scroll down" is ambiguous on a page with an inner
    pane, and picking wrong wastes a step every time.
    """

    label: str
    box: tuple[int, int, int, int]
    scroll_top: int
    scroll_height: int
    client_height: int
    path: list[dict[str, str]] = field(default_factory=list)

    @property
    def progress(self) -> float:
        span = max(self.scroll_height - self.client_height, 1)
        return min(self.scroll_top / span, 1.0)

    @classmethod
    def from_js(cls, d: dict[str, Any]) -> Self:
        box = d.get("box") or [0, 0, 0, 0]
        return cls(
            label=d.get("label", ""),
            box=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
            scroll_top=int(d.get("scrollTop", 0)),
            scroll_height=int(d.get("scrollHeight", 0)),
            client_height=int(d.get("clientHeight", 0)),
            path=d.get("path") or [],
        )


@dataclass(slots=True)
class Viewport:
    w: int
    h: int
    scroll_x: int
    scroll_y: int
    doc_w: int
    doc_h: int

    @property
    def scroll_progress(self) -> float:
        span = max(self.doc_h - self.h, 1)
        return min(self.scroll_y / span, 1.0)

    @property
    def has_more_below(self) -> bool:
        return self.scroll_y + self.h < self.doc_h - 4

    @classmethod
    def from_js(cls, d: dict[str, Any]) -> Self:
        return cls(
            w=int(d.get("w", 0)),
            h=int(d.get("h", 0)),
            scroll_x=int(d.get("scrollX", 0)),
            scroll_y=int(d.get("scrollY", 0)),
            doc_w=int(d.get("docW", 0)),
            doc_h=int(d.get("docH", 0)),
        )


@dataclass(slots=True)
class Snapshot:
    """Everything one agent step knows about the page."""

    url: str
    title: str
    viewport: Viewport
    elements: list[Element]
    scrollables: list[Scrollable]
    content: str = ""  # readable text, from reader.py
    ready_state: str = "complete"
    stats: dict[str, Any] = field(default_factory=dict)
    tab_id: str = ""
    open_tabs: list[dict[str, str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    # Ref lookup index. Declared as a field because `slots=True` derives __slots__
    # from the field list — an undeclared attribute cannot be assigned.
    _by_ref: dict[str, Element] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_ref = {e.ref: e for e in self.elements}

    def get(self, ref: str) -> Element | None:
        return self._by_ref.get(ref)

    def find(self, text: str, *, role: str | None = None) -> list[Element]:
        """Substring match on accessible name — for tests and for resolving a model
        that named a control instead of citing its ref."""
        needle = text.strip().lower()
        return [
            e
            for e in self.elements
            if needle in e.name.lower() and (role is None or e.role == role)
        ]
