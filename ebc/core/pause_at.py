"""Pause at frame: when to pause the emulation, from the game frame the sync tick reads. No ``bpy``.

The frame is ``MATCH_FRAME`` (frames since GO: 0 in menus and during the intro, frozen in pause).
The trigger fires on the tick where the frame **crosses** the target (``last < target <= frame``),
so it fires once per pass: not when the sync starts past the target, not again while paused on it
or after the emulation resumes. A new match (or a stage change) resets it; a savestate that brings
the frame back before the target arms it again, so the same pause can be replayed. How the
emulation is paused is not decided here (``ui.sync.request_pause``).
"""

from __future__ import annotations


def crossed(last: int | None, frame: int | None, target: int) -> bool:
    """The frame went from below ``target`` to ``target`` or beyond (``target`` <= 0: off)."""
    return target > 0 and last is not None and frame is not None and last < target <= frame


class PauseAtFrame:
    """Remembers the last in-match frame between ticks; ``check`` says when to pause."""

    def __init__(self) -> None:
        self.last_frame: int | None = None

    def reset(self) -> None:
        """A new match or another stage: nothing of the previous frames counts."""
        self.last_frame = None

    def check(self, frame: int | None, in_match: bool, target: int) -> bool:
        """Call once per tick. True on the tick the frame crosses ``target`` in a match."""
        if not in_match or frame is None:
            self.last_frame = None
            return False
        fire = crossed(self.last_frame, frame, target)
        self.last_frame = frame
        return fire
