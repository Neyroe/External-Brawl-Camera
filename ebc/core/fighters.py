"""The fighters of the match: resolution (entry -> active instance), positions, and the per-fighter
mode (normal, colour tint, hidden). No ``bpy``.

Colour: soColorBlendModule "sub colour", RGBA at cbm+0x14A, enable at cbm+0x14F (what setSubColor
writes, nothing else). Hidden: soVisibilityModule+0x0E = 0. A KO/respawn resets both, so they are
written on every sync tick while COLOUR/HIDDEN (``refresh``, skipped when the game still holds them)
and cleared once on NORMAL. Every pointer is range-checked and every module guarded by its back
pointer to the soModuleAccesser.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .addresses import is_valid_pointer
from .camera import to_blender
from .overrides import color_to_bytes

if TYPE_CHECKING:
    from .game import Game

Vec3 = tuple[float, float, float]
_CLEAR = b"\x00\x00\x00\x00"


class FighterMode(StrEnum):
    NORMAL = "NORMAL"  # untouched
    COLOUR = "COLOUR"  # sub-colour tint (colour + alpha)
    HIDDEN = "HIDDEN"  # not drawn (soVisibilityModule)


FIGHTER_NORMAL = FighterMode.NORMAL
FIGHTER_COLOUR = FighterMode.COLOUR
FIGHTER_HIDDEN = FighterMode.HIDDEN
FIGHTER_MODES = tuple(FighterMode)


@dataclass
class FighterRef:
    """One fighter, resolved like ftManager::getFighter (entry -> active instance)."""

    port: int  # 0-based
    entry: int
    kind: int
    fighter: int
    modules: int
    # soModuleAccesser of every instance to tint/hide (the active one, plus
    # Nana for the Ice Climbers).
    all_modules: list[int] = field(default_factory=list)


@dataclass
class _FighterMode:
    mode: str = FIGHTER_NORMAL
    rgba: bytes = _CLEAR


def isolated_slot(modes: Sequence[str]) -> int | None:
    """The slot shown alone (every other slot HIDDEN, itself not), else None."""
    for slot, mode in enumerate(modes):
        if mode != FIGHTER_HIDDEN and all(m == FIGHTER_HIDDEN for i, m in enumerate(modes) if i != slot):
            return slot
    return None


def toggle_isolation(modes: Sequence[str], slot: int) -> tuple[list[str], bool]:
    """Modes after pressing Isolate on ``slot``, and whether it isolates (False = undo).

    Isolating hides every other slot and shows ``slot`` (HIDDEN becomes NORMAL,
    COLOUR is kept). Pressing it again on the isolated slot shows the others.
    """
    if isolated_slot(modes) == slot:
        return [m if i == slot else FIGHTER_NORMAL for i, m in enumerate(modes)], False
    new = [FIGHTER_HIDDEN] * len(modes)
    new[slot] = FIGHTER_NORMAL if modes[slot] == FIGHTER_HIDDEN else modes[slot]
    return new, True


class FighterRoster:
    """``Game.roster``: the fighters and what EBC does to them."""

    def __init__(self, game: Game) -> None:
        self.game = game
        self.modes: dict[int, _FighterMode] = {}  # wanted per port (kept across matches)
        # fighter channels we wrote: port -> {module address: soModuleAccesser}
        self._painted: dict[int, dict[int, int]] = {}
        self._hidden: dict[int, dict[int, int]] = {}

    def forget(self) -> None:
        """New fighters (new match, new connection): nothing of ours is left on them."""
        self._painted.clear()
        self._hidden.clear()

    def resolve(self) -> dict[int, FighterRef]:
        """Every fighter in the match, by 0-based port. Resolve once per tick.

        Same chain as ftManager::getFighter: entry -> m_instances[active].
        Entries are in creation order, so the port comes from the entry.
        Zelda/Sheik and Pokemon Trainer switch instance: resolving every
        tick follows them. Every pointer is range-checked.
        """
        p = self.game.p
        entries = self.game.deref(p.FT_ENTRY_MANAGER.addr)
        if entries is None:
            return {}
        # Only the used part of the table: count, active, port and the instances of each entry
        # (+0x08..+0x48 of 0x244), i.e. from entry 0 +0x08 to the last entry +0x48.
        first = p.ENTRY_INSTANCE_COUNT.offset
        last = p.ENTRY_INSTANCES.offset + p.ENTRY_MAX_INSTANCES * p.ENTRY_INSTANCE_STRIDE
        used = self.game.read_bytes(entries + first, (p.FT_ENTRY_COUNT - 1) * p.FT_ENTRY_STRIDE + last - first)
        if used is None:
            return {}
        data = bytes(first) + used  # offsets below stay relative to the table start
        refs: dict[int, FighterRef] = {}
        for i in range(p.FT_ENTRY_COUNT):
            base = i * p.FT_ENTRY_STRIDE
            (port,) = struct.unpack_from(p.ENTRY_PORT.fmt, data, base + p.ENTRY_PORT.offset)
            if not 0 <= port < p.FT_ENTRY_COUNT or port in refs:
                continue
            count = min(data[base + p.ENTRY_INSTANCE_COUNT.offset], p.ENTRY_MAX_INSTANCES)
            (active,) = struct.unpack_from(p.ENTRY_ACTIVE_INSTANCE.fmt, data, base + p.ENTRY_ACTIVE_INSTANCE.offset)
            if not 0 <= active < count:
                continue
            instances = []
            for k in range(count):
                kind, fighter = struct.unpack_from(
                    p.ENTRY_INSTANCES.fmt, data, base + p.ENTRY_INSTANCES.offset + k * p.ENTRY_INSTANCE_STRIDE
                )
                instances.append((kind, fighter))
            kind, fighter = instances[active]
            if not is_valid_pointer(fighter):
                continue
            modules = self.game.deref(fighter + p.FIGHTER_MODULES.offset)
            if modules is None:
                continue
            ref = FighterRef(port, entries + base, kind, fighter, modules, [modules])
            if kind in (p.KIND_POPO, p.KIND_NANA):
                # Ice Climbers (deduced): the partner instance is active too.
                for k, (other_kind, other) in enumerate(instances):
                    if k != active and other_kind in (p.KIND_POPO, p.KIND_NANA) and is_valid_pointer(other):
                        other_modules = self.game.deref(other + p.FIGHTER_MODULES.offset)
                        if other_modules is not None:
                            ref.all_modules.append(other_modules)
            refs[port] = ref
        return refs

    def ref(self, port: int) -> FighterRef | None:
        return self.resolve().get(port)

    def fighter_address(self, port: int) -> int | None:
        ref = self.ref(port)
        return None if ref is None else ref.fighter

    def character_id(self, port: int) -> int | None:
        """ftKind of the active form (0 = Mario is valid), None for an empty port."""
        ref = self.ref(port)
        return None if ref is None else ref.kind

    def is_player_present(self, port: int) -> bool:
        return self.ref(port) is not None

    def position(self, port: int, refs: dict[int, FighterRef] | None = None) -> Vec3 | None:
        """Ground position of the fighter in Blender space, or None."""
        ref = (self.resolve() if refs is None else refs).get(port)
        if ref is None:
            return None
        p = self.game.p
        posture = self.game.deref(ref.modules + p.MODULES_POSTURE.offset)
        if posture is None:
            return None
        data = self.game.read_bytes(posture + p.POSTURE_POSITION.offset, p.POSTURE_POSITION.size)
        if data is None:
            return None
        xyz = struct.unpack(p.POSTURE_POSITION.fmt, data)
        if not all(math.isfinite(c) and abs(c) < 1e6 for c in xyz):
            return None
        return to_blender(xyz)

    def _guarded_module(self, modules: int, slot_offset: int, guard_offset: int) -> int | None:
        module = self.game.deref(modules + slot_offset)
        if module is None or self.game.read_u32(module + guard_offset) != modules:
            return None  # not the object we think it is: write nothing
        return module

    def color_blend_module(self, port: int) -> int | None:
        ref = self.ref(port)
        if ref is None:
            return None
        return self._guarded_module(
            ref.modules, self.game.p.MODULES_COLOR_BLEND.offset, self.game.p.CBM_ACCESSER.offset
        )

    def visibility_module(self, port: int) -> int | None:
        ref = self.ref(port)
        if ref is None:
            return None
        p = self.game.p
        return self._guarded_module(ref.modules, p.MODULES_VISIBILITY.offset, p.VIS_ACCESSER.offset)

    # -- per-fighter mode: normal, colour, hidden ---------------------------------------

    def set_mode(self, port: int, mode: str, rgba: Sequence[float] = (0.0, 0.0, 0.0, 0.0)) -> bool:
        """NORMAL (untouched), COLOUR (``rgba`` floats 0..1, alpha = tint strength,
        1 = flat colour) or HIDDEN (not drawn)."""
        if mode not in FIGHTER_MODES:
            raise ValueError(f"unknown fighter mode {mode!r}")
        state = self.modes.setdefault(port, _FighterMode())
        state.mode = mode
        state.rgba = color_to_bytes(rgba)
        self.refresh()
        return self.game.connected

    def needs_refresh(self) -> bool:
        return (
            any(m.mode != FIGHTER_NORMAL for m in self.modes.values())
            or any(self._painted.values())
            or any(self._hidden.values())
        )

    def refresh(self, refs: dict[int, FighterRef] | None = None) -> int:
        """Apply colours/hides to the current fighters. Call every tick. Returns the writes made."""
        if not self.game.connected or not self.needs_refresh():
            return 0
        if refs is None:
            refs = self.resolve()
        p = self.game.p
        writes = 0
        for port in range(p.FT_ENTRY_COUNT):
            state = self.modes.get(port, _FighterMode())
            ref = refs.get(port)
            color = state.rgba if state.mode == FIGHTER_COLOUR else None
            hide = state.mode == FIGHTER_HIDDEN
            painted = self._painted.setdefault(port, {})
            hidden = self._hidden.setdefault(port, {})
            for modules in ref.all_modules if ref is not None else ():
                cbm = self._guarded_module(modules, p.MODULES_COLOR_BLEND.offset, p.CBM_ACCESSER.offset)
                if cbm is not None and color is not None:
                    # every tick (a KO/respawn resets the channel), sent only when it differs
                    self.game.write_bytes(cbm + p.CBM_SUB_COLOR.offset, color, skip_same=True)
                    self.game.write_bytes(cbm + p.CBM_SUB_ENABLE.offset, b"\x01", skip_same=True)
                    painted[cbm] = modules
                    writes += 1
                vis = self._guarded_module(modules, p.MODULES_VISIBILITY.offset, p.VIS_ACCESSER.offset)
                if vis is not None and hide:
                    self.game.write_bytes(vis + p.VIS_VISIBLE.offset, b"\x00", skip_same=True)
                    hidden[vis] = modules
                    writes += 1
            if color is None:
                writes += self._clear_painted(painted)
            if not hide:
                writes += self._clear_hidden(hidden)
        return writes

    def _still_ours(self, module: int, guard_offset: int, modules: int) -> bool:
        return self.game.read_u32(module + guard_offset) == modules

    def _clear_painted(self, painted: dict[int, int]) -> int:
        p = self.game.p
        n = 0
        for cbm, modules in list(painted.items()):
            if self._still_ours(cbm, p.CBM_ACCESSER.offset, modules):
                self.game.write_bytes(cbm + p.CBM_SUB_COLOR.offset, _CLEAR)
                self.game.write_bytes(cbm + p.CBM_SUB_ENABLE.offset, b"\x00")
                n += 1
            del painted[cbm]
        return n

    def _clear_hidden(self, hidden: dict[int, int]) -> int:
        p = self.game.p
        n = 0
        for vis, modules in list(hidden.items()):
            if self._still_ours(vis, p.VIS_ACCESSER.offset, modules):
                self.game.write_bytes(vis + p.VIS_VISIBLE.offset, b"\x01")  # once: never force 1 in a loop
                n += 1
            del hidden[vis]
        return n

    def clear(self) -> bool:
        if not self.game.connected:
            return False
        for painted in self._painted.values():
            self._clear_painted(painted)
        for hidden in self._hidden.values():
            self._clear_hidden(hidden)
        return True
