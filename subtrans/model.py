"""The interchange type every part of subtrans speaks.

Readers turn files into a `Subtitle`; the translator rewrites `Cue.lines` and nothing
else; writers turn a `Subtitle` back into a file. Timing is milliseconds throughout —
frames only exist at the Resolve boundary, and that conversion lives in the plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterator

# Resolve stores an in-cue line break as U+2028 LINE SEPARATOR. Subtitle files use "\n".
# Written as an escape on purpose: the literal character is invisible in an editor.
LINE_SEPARATOR = "\u2028"


@dataclass
class Cue:
    """One subtitle event."""

    index: int
    start_ms: int
    end_ms: int
    lines: list[str] = field(default_factory=list)
    style: str | None = None
    speaker: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def with_text(self, text: str) -> "Cue":
        """A copy carrying new text. Timing is never touched by translation."""
        return replace(self, lines=[ln.rstrip() for ln in text.split("\n") if ln.strip()] or [""])

    def chars_per_second(self) -> float:
        secs = self.duration_ms / 1000
        return len(self.text.replace("\n", "")) / secs if secs > 0 else 0.0


@dataclass
class Subtitle:
    """An ordered set of cues plus whatever the source format told us about them."""

    cues: list[Cue] = field(default_factory=list)
    language: str | None = None
    source_format: str | None = None

    def __iter__(self) -> Iterator[Cue]:
        return iter(self.cues)

    def __len__(self) -> int:
        return len(self.cues)

    def renumber(self) -> "Subtitle":
        for n, cue in enumerate(self.cues, 1):
            cue.index = n
        return self

    def sorted(self) -> "Subtitle":
        self.cues.sort(key=lambda c: (c.start_ms, c.end_ms))
        return self
