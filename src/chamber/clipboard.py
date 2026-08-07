"""Chamber's own clipboard.

Deliberately **not** the operating system's. An agent copying a product title must
not wipe whatever the person at the keyboard had on their real clipboard — the same
principle as the synthetic cursor: the machine stays theirs while the agent works.

The other half of the point is token cost. Text copied here goes page → buffer →
page without ever entering the model's context. Carrying five Amazon listing titles
to another site costs nothing and is character-exact, where asking the model to
remember and re-type them costs tokens twice and quietly loses details.

Its own module rather than living in `session.py` because `actions/executor.py`
needs the type, and `session.py` already imports the executor — the direct import
was a circular one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Clip:
    """One piece of text lifted off a page."""

    text: str
    label: str = ""
    source_url: str = ""

    def preview(self, width: int = 70) -> str:
        """A short, single-line rendering — what the model is told it copied.

        The model gets this, never `text`. Reporting the full content back would
        undo the entire saving.
        """
        flat = " ".join(self.text.split())
        return flat if len(flat) <= width else flat[: width - 1] + "…"


@dataclass(slots=True)
class Clipboard:
    """An ordered set of clips, addressable by label."""

    clips: list[Clip] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.clips)

    def __bool__(self) -> bool:
        return bool(self.clips)

    def __iter__(self):
        return iter(self.clips)

    def add(self, text: str, *, label: str = "", source_url: str = "") -> Clip:
        """Append a clip. A repeated label replaces the earlier one, so re-copying
        after a correction does not leave both versions to be pasted."""
        if label:
            self.clips = [c for c in self.clips if c.label != label]
        clip = Clip(text=text, label=label, source_url=source_url)
        self.clips.append(clip)
        return clip

    def select(self, labels: list[str]) -> tuple[list[Clip], list[str]]:
        """Clips matching `labels`, plus any labels that matched nothing.

        Returning the misses rather than silently dropping them is what lets the
        executor tell the model which label it invented.
        """
        if not labels:
            return list(self.clips), []
        known = {c.label for c in self.clips}
        return [c for c in self.clips if c.label in labels], sorted(set(labels) - known)

    def render(self, clips: list[Clip], separator: str = "\n") -> str:
        return separator.join(c.text for c in clips)

    def clear(self) -> int:
        count = len(self.clips)
        self.clips.clear()
        return count

    def summary(self) -> list[str]:
        return [
            f"{i}. {c.label or '(unlabelled)'} — {len(c.text)} chars: {c.preview()!r}"
            for i, c in enumerate(self.clips, 1)
        ]
