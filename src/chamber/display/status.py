"""Small helpers for adding a status source without touching Chamber Desk."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from chamber.display.adapter import StatusSignal, StatusUpdate


@dataclass(slots=True, frozen=True)
class EventStatusGetter:
    """Match a named event family and pass its payload to a parser."""

    events: frozenset[str]
    source: str = "custom"

    def __init__(self, *events: str, source: str = "custom") -> None:
        object.__setattr__(self, "events", frozenset(events))
        object.__setattr__(self, "source", source)

    def get(self, event: str, payload: Mapping[str, Any]) -> StatusSignal | None:
        if event not in self.events:
            return None
        return StatusSignal(event=event, payload=payload, source=self.source)


@dataclass(slots=True, frozen=True)
class FunctionStatusParser:
    """Turn a signal into a :class:`StatusUpdate` with one small function."""

    function: Callable[[StatusSignal], StatusUpdate | None]

    def parse(self, signal: StatusSignal) -> StatusUpdate | None:
        return self.function(signal)
