"""Values EBC overrides in the game's memory, and how they are put back (docs/DEVELOPMENT.md
"Adding a memory address", 4). No ``bpy``.

``Override``: a range written by EBC, with the game's original kept for the restore.
``BitOverride``: one bit of a flags byte the game also owns (read-modify-write, never from the tick
snapshot). ``write_bit`` / ``set_bit``: the one read-modify-write of a bit, also used where EBC
changes a bit without keeping it (intro skip).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .game import Game as MemorySession


def color_to_bytes(rgba: Sequence[float]) -> bytes:
    """Float colour (0..1, already gamma-encoded / sRGB) -> RGBA bytes."""
    vals = list(rgba) + [1.0] * (4 - len(rgba))
    return bytes(max(0, min(255, round(float(c) * 255))) for c in vals[:4])


class Override:
    """A memory range we overwrite, with the game's original value kept for restore.

    The original is captured on the first ``apply``. It is re-captured when
    the game has rewritten the range behind our back (new match, new stage):
    the value found then is the game's new default, not ours. ``retarget``
    follows a structure that moved (the stage FOV lives in the stage heap).
    """

    def __init__(self, addr: int, size: int) -> None:
        self.addr = addr
        self.size = size
        self.original: bytes | None = None
        self.written: bytes | None = None

    @property
    def active(self) -> bool:
        return self.original is not None

    def retarget(self, addr: int) -> None:
        if addr != self.addr:
            self.addr = addr
            self.forget()  # the old object is gone: nothing to restore there

    def apply(self, game: MemorySession, data: bytes, check: bool = True, skip_same: bool = False) -> bool:
        if self.original is None or check:
            current = game.read_bytes(self.addr, self.size)
            if current is None:
                return False
            if self.original is None or (self.written is not None and current != self.written):
                self.original = current
        if not game.write_bytes(self.addr, data, skip_same=skip_same):
            return False
        self.written = bytes(data)
        return True

    def restore(self, game: MemorySession) -> bool:
        if self.original is None:
            return True
        if not game.write_bytes(self.addr, self.original):
            return False
        self.forget()
        return True

    def forget(self) -> None:
        self.original = None
        self.written = None

    def adopt(self, original: bytes, written: bytes) -> None:
        """Bytes written by someone else on our behalf (a bigger block, a projection kept aside):
        ``original`` is kept unless one is already remembered, ``written`` is what is there now."""
        if self.original is None:
            self.original = bytes(original)
        self.written = bytes(written)


def write_bit(game: MemorySession, addr: int, mask: int, on: bool, fresh: bool = True) -> tuple[bool, int | None]:
    """Set or clear ``mask`` in the byte at ``addr`` by read-modify-write: (written or already so, the
    byte as found). ``fresh``: read from the backend, never from the tick snapshot (a byte the game
    also owns). Nothing is written when the bit is already as wanted."""
    data = game.read_bytes(addr, 1, fresh=fresh)
    if data is None:
        return False, None
    current = data[0]
    new = current | mask if on else current & ~mask & 0xFF
    return new == current or game.write_bytes(addr, bytes([new])), current


def set_bit(game: MemorySession, addr: int, mask: int, on: bool, fresh: bool = True) -> bool:
    return write_bit(game, addr, mask, on, fresh)[0]


class BitOverride:
    """One bit of a flags byte, set or cleared by read-modify-write; the original bit is restored."""

    def __init__(self, addr: int, bit: int) -> None:
        self.addr = addr
        self.bit = bit
        self.original: bool | None = None

    @property
    def active(self) -> bool:
        return self.original is not None

    def retarget(self, addr: int) -> None:
        if addr != self.addr:
            self.addr = addr
            self.original = None  # another object: nothing to restore there

    def capture(self, game: MemorySession) -> bool:
        """Remember the original bit now (before something else changes it)."""
        if self.original is None:
            data = game.read_bytes(self.addr, 1, fresh=True)
            if data is None:
                return False
            self.original = bool(data[0] & self.bit)
        return True

    def is_on(self, byte: int) -> bool:
        return bool(byte & self.bit)

    def apply(self, game: MemorySession, on: bool, original: bool | None = None) -> bool:
        """Set or clear the bit. ``original``: the value to restore when it is captured now, instead of
        the bit as found (the game itself holds it in another state at that moment)."""
        ok, found = write_bit(game, self.addr, self.bit, on)
        if found is not None and self.original is None:
            self.original = self.is_on(found) if original is None else original
        return ok

    def restore(self, game: MemorySession) -> bool:
        if self.original is None:
            return True
        if not self.apply(game, self.original):
            return False
        self.original = None
        return True

    def forget(self) -> None:
        self.original = None
