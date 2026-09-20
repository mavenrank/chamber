"""The custom Chamber Desk display.

The display is deliberately separate from the browser overlay.  The overlay stays
in the project as a browser-local cursor/control tether; this package owns the
small, platform-neutral window that is the primary run read-out.
"""

from chamber.display.adapter import (
    DisplayAdapter,
    DisplayState,
    StatusAdapter,
    StatusGetter,
    StatusParser,
    StatusSignal,
    StatusUpdate,
)
from chamber.display.queries import (
    build_session_block,
    get_environment,
    get_run,
    list_profiles,
    list_runs,
)
from chamber.display.status import EventStatusGetter, FunctionStatusParser
from chamber.display.window import ChamberDesk

__all__ = [
    "ChamberDesk",
    "DisplayAdapter",
    "DisplayState",
    "EventStatusGetter",
    "FunctionStatusParser",
    "StatusAdapter",
    "StatusGetter",
    "StatusParser",
    "StatusSignal",
    "StatusUpdate",
    "build_session_block",
    "get_environment",
    "get_run",
    "list_profiles",
    "list_runs",
]
