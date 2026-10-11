"""Full-screen background: EBC's own efScreen fill request (docs/research/green-screen-alpha.md).
No ``bpy``.

The game's screen-fill manager (efScreen, the one
that dims the screen during a Final Smash) draws layer 0 after the stage pass, under the
fighters, items, effects and HUD. EBC writes its own request (n° 9, the last one: the game
allocates from 0) as a held flat fill and chains it in layer 0: its alpha is exact
(src * A + dst * (1 - A) over the stage), A = 255 is a flat green screen with the stage
still drawn. Data only, no code patch. The game empties the layer when a match ends and a
savestate brings its own: ``hold`` repairs it on every sync tick, for free when it holds
(the request and the layer are in the tick snapshot). Never written outside a match (g_Stage
null).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .addresses import MEM1_END, MEM1_START
from .overrides import color_to_bytes

if TYPE_CHECKING:
    from .game import Game

# efScreen request 9 as EBC writes it (green-screen-alpha.md 2): +0x01 active, +0x03 layer 0,
# +0x04 priority FF (above a Final Smash dimming), +0x05 type 2 (flat fill), +0x08 auto-end 0
# (held for ever), duration and elapsed 0 (colour at once); then 3 x RGBA (start, target, current).
_FILL_HEAD = bytes([0x00, 0x01, 0x00, 0x00, 0xFF, 0x02, 0x00, 0x00]) + bytes(12)
_FREE_SLOT = 0xFF


class GreenScreen:
    """``Game.fill``: request 9 laid in efScreen layer 0, its original kept for the release."""

    def __init__(self, game: Game) -> None:
        self.game = game
        self._color: bytes | None = None  # fill colour (RGBA), None = off
        self._base: int | None = None  # efScreen the original of request 9 was read from
        self._original: bytes | None = None  # request 9 as the game had it

    def forget(self) -> None:
        """A new connection: nothing of ours is known to be in place (the colour is kept)."""
        self._base = None
        self._original = None

    def reapply(self) -> bool:
        """New match / Re-apply: lay it again when it is on."""
        return True if self._color is None else self.apply()

    def set(self, enabled: bool, rgba: Sequence[float]) -> bool:
        """Full-screen background: a flat ``rgba`` fill over the stage, under the fighters.

        The alpha is effective (0 = invisible, 1 = opaque). Outside a match the colour is kept
        and the layer is laid at the next tick in a match. Off: request 9 is unchained and
        given back as it was.
        """
        if not enabled:
            self._color = None
            return self.release()
        self._color = color_to_bytes(rgba)
        return self.apply()

    def hold(self, in_match: bool | None) -> bool:
        """Per tick: lay the fill again if the game dropped it (end of match, savestate).

        ``in_match``: the g_Stage check of this tick's ``poll``; served by the tick snapshot, so it
        costs no call while the layer holds.
        """
        if self._color is None:
            return True
        return self.apply(in_match, held=True)

    @property
    def enabled(self) -> bool:
        return self._color is not None

    def _request(self, base: int) -> int:
        p = self.game.p
        return base + p.EFSCREEN_REQUESTS.offset + p.EFSCREEN_REQUEST_STRIDE * p.EFSCREEN_REQUEST_INDEX

    def _efscreen(self) -> int | None:
        base = self.game.deref(self.game.p.EFSCREEN_PTR.addr)
        if base is None or not MEM1_START <= base < MEM1_END:
            return None
        return base

    def _is_ours(self, data: bytes, at: int) -> bool:
        """Request 9 is ours and in place (active held flat fill, layer 0, priority FF) and chained."""
        p = self.game.p
        request = data[at : at + 8]
        return (
            p.EFSCREEN_REQUEST_INDEX in data[1 : 1 + p.EFSCREEN_SLOTS]
            and request[p.REQ_ACTIVE.offset] == 1
            and request[p.REQ_TYPE.offset] == 2
            and request[3:5] == _FILL_HEAD[3:5]
        )

    def apply(self, in_match: bool | None = None, held: bool = False) -> bool:
        color = self._color
        if color is None or not self.game.connected:
            return False
        if in_match is None:
            in_match = self.game.stage_pointer() is not None
        if not in_match:
            return True  # menus: nothing to draw on, laid again in the next match
        p = self.game.p
        size = self._request(0) + p.EFSCREEN_REQUEST_STRIDE  # layer 0 .. request 9: one read
        data = None
        base = self._base if held else None
        if base is not None:
            # Per tick, while it holds: our own request found where we laid it this session
            # (its signature checked on this tick's bytes) proves the efScreen there is still
            # the live one, without reading g_efScreen again. Anything else: full path below.
            data = self.game.read_bytes(base, size)
            if data is None or not self._is_ours(data, size - p.EFSCREEN_REQUEST_STRIDE):
                data = None
        if data is None:
            base = self._efscreen()
            if base is None:
                return False
            data = self.game.read_bytes(base, size)
            if data is None:
                return False
        req = self._request(base)
        at = req - base
        if self._base != base:
            self._base = base
            self._original = data[at : at + p.EFSCREEN_REQUEST_STRIDE]
        colors = color * 3
        linked = p.EFSCREEN_REQUEST_INDEX in data[1 : 1 + p.EFSCREEN_SLOTS]
        if self._is_ours(data, at):
            # in place: only the colour, sent when it differs from what the game holds
            return self.game.write_bytes(req + p.REQ_COLORS.offset, colors, skip_same=True)
        if not linked and _FREE_SLOT not in data[1 : 1 + p.EFSCREEN_SLOTS]:
            self.game.last_error = "efScreen layer 0 is full"
            return False  # nothing written: the request is not laid without a slot for it
        ok = self.game.write_bytes(req, _FILL_HEAD + colors)
        if not linked:
            ok = ok and self._link(base)
        return ok

    def _link(self, base: int) -> bool:
        """Chain request 9 in the first free slot of layer 0 (count + 1)."""
        p = self.game.p
        layer = self.game.read_bytes(base, p.EFSCREEN_LAYER0.size, fresh=True)  # the game owns it
        if layer is None:
            return False
        slots = list(layer[1:])
        if p.EFSCREEN_REQUEST_INDEX in slots:
            return True
        if _FREE_SLOT not in slots:
            self.game.last_error = "efScreen layer 0 is full"
            return False
        slots[slots.index(_FREE_SLOT)] = p.EFSCREEN_REQUEST_INDEX
        return self.game.write_bytes(base, bytes([(layer[0] + 1) & 0xFF, *slots]))

    def release(self) -> bool:
        """Unchain request 9 (shifting the next slots, count - 1) and give it back.

        The layer is never rewritten from a copy: a Final Smash may have chained itself since.
        The request is only given back while it is ours (chained by us, or free again).
        """
        base = self._base
        if base is None:
            return True
        if not self.game.connected:
            return False
        p = self.game.p
        if self._efscreen() != base:
            self._base = self._original = None  # another efScreen: nothing of ours
            return True
        layer = self.game.read_bytes(base, p.EFSCREEN_LAYER0.size, fresh=True)
        if layer is None:
            return False
        index = p.EFSCREEN_REQUEST_INDEX
        slots = list(layer[1:])
        ok = True
        linked = index in slots
        if linked:
            k = slots.index(index)
            slots = [*slots[:k], *slots[k + 1 :], _FREE_SLOT]
            ok = self.game.write_bytes(base, bytes([(layer[0] - 1) & 0xFF, *slots]))
        req = self._request(base)
        request = self.game.read_bytes(req, p.EFSCREEN_REQUEST_STRIDE, fresh=True)
        if ok and request is not None and (linked or request[p.REQ_ACTIVE.offset] == 0):
            ok = self.game.write_bytes(req + p.REQ_ACTIVE.offset, b"\x00")
            original = self._original
            if ok and original is not None:
                # the game's bytes, free (active 0: 0xCC on a fresh memory would keep it reserved)
                ok = self.game.write_bytes(req, original[:1] + b"\x00" + original[2:])
        if ok:
            self._base = self._original = None
        return ok
