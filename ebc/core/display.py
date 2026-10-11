"""What the game shows and plays, as the user set it: Code Menu display toggles, characters and
stage visibility, void colour, music and sound volume, shadow light direction. No ``bpy``.

Every value goes through the Game's overrides (restored by ``restore_all``) and is remembered here
to be written again on a new match (``Game.reapply_persistent``; the game resets the shadows on a
stage load, the music on the character select screen).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from .addresses import Field
from .overrides import color_to_bytes

if TYPE_CHECKING:
    from .game import Game

# -- modes shared with the UI ---------------------------------------------------------


class CharactersMode(StrEnum):
    MODEL = "MODEL"
    MODEL_HURTBOXES = "MODEL_HURTBOXES"
    HURTBOXES = "HURTBOXES"
    HIDDEN = "HIDDEN"


class StageMode(StrEnum):
    VISIBLE = "VISIBLE"
    VISIBLE_COLLISION = "VISIBLE_COLLISION"
    COLLISION_ONLY = "COLLISION_ONLY"
    HIDDEN = "HIDDEN"


CHARACTERS_MODEL = CharactersMode.MODEL
CHARACTERS_MODEL_HURTBOXES = CharactersMode.MODEL_HURTBOXES
CHARACTERS_HURTBOXES = CharactersMode.HURTBOXES
CHARACTERS_HIDDEN = CharactersMode.HIDDEN

STAGE_VISIBLE = StageMode.VISIBLE
STAGE_VISIBLE_COLLISION = StageMode.VISIBLE_COLLISION
STAGE_COLLISION_ONLY = StageMode.COLLISION_ONLY
STAGE_HIDDEN = StageMode.HIDDEN


# Code Menu hurtbox value, characters group visible
_CHARACTERS_MODES = {
    CHARACTERS_MODEL: (0, True),
    CHARACTERS_MODEL_HURTBOXES: (1, True),
    CHARACTERS_HURTBOXES: (2, True),  # value 2 hides the models by itself
    CHARACTERS_HIDDEN: (0, False),
}
# Code Menu collision value, stage group visible
_STAGE_MODES = {
    STAGE_VISIBLE: (0, True),
    STAGE_VISIBLE_COLLISION: (1, True),
    STAGE_COLLISION_ONLY: (2, True),  # value 2 hides the background by itself
    STAGE_HIDDEN: (0, False),
}


class DisplaySettings:
    """``Game.display``."""

    def __init__(self, game: Game) -> None:
        self.game = game
        # desired state (None = never set by the user)
        self._music: float | None = None
        self._sfx: float | None = None
        self._shadow_dir: list[float | None] = [None, None]
        self._flags: dict[str, int] = {}  # Code Menu toggles by field name (kept once touched)
        self._characters: str | None = None
        self._stage: str | None = None
        self._background: bytes | None = None  # void colour (EFB clear colour)

    # -- Code Menu toggles (restored on disconnect) -----------------------------------

    def set_flag(self, field: Field, value: int | bool) -> bool:
        self._flags[field.name] = int(value)
        return self.game.ov[field.name].apply(self.game, bytes([int(value)]))

    def reapply_flags(self) -> None:
        for name, value in self._flags.items():
            self.game.ov[name].apply(self.game, bytes([value]))

    def set_hud(self, enabled: bool) -> bool:
        return self.set_flag(self.game.p.DISPLAY_HUD, enabled)

    def set_draw_di(self, enabled: bool) -> bool:
        return self.set_flag(self.game.p.DRAW_DI, enabled)

    # -- characters / stage visibility (persist across matches: restored) ----------

    def set_characters_display(self, mode: str) -> bool:
        if mode not in _CHARACTERS_MODES:
            return False
        self._characters = mode
        return self._apply_characters()

    def _apply_characters(self) -> bool:
        hurtbox, visible = _CHARACTERS_MODES[self._characters or CHARACTERS_MODEL]
        p = self.game.p
        # Measured: hurtbox value 2 clears this same bit, and the 2 -> 0
        # transition sets it again on the next frame. Capture the original
        # before touching the hurtbox value; HIDDEN is re-asserted per tick.
        self.game.bit_characters.capture(self.game)
        ov = self.game.ov[p.DISPLAY_HURTBOX.name]
        ok = ov.apply(self.game, bytes([hurtbox])) if hurtbox else ov.restore(self.game)
        bit = self.game.bit_characters
        if not visible:
            return bit.apply(self.game, False) and ok
        if hurtbox == 2:
            return ok  # the game hides the models itself: keep the captured original
        return bit.restore(self.game) and ok

    def reapply_characters(self) -> None:
        if self._characters is not None:
            self._apply_characters()

    def assert_display(self) -> bool:
        """Per-tick: keep the characters hidden (the game may set the bit back, see above)."""
        if self._characters != CHARACTERS_HIDDEN:
            return True
        return self.game.bit_characters.apply(self.game, False)

    def set_stage_display(self, mode: str) -> bool:
        if mode not in _STAGE_MODES:
            return False
        self._stage = mode
        return self.apply_stage()

    def apply_stage(self) -> bool:
        collision, visible = _STAGE_MODES[self._stage or STAGE_VISIBLE]
        ov = self.game.ov[self.game.p.DISPLAY_STAGE_COLLISION.name]
        ok = ov.apply(self.game, bytes([collision])) if collision else ov.restore(self.game)
        bit = self.game.bit_stage
        return (bit.apply(self.game, False) if not visible else bit.restore(self.game)) and ok

    # -- audio (persistent, restored on disconnect) -------------------------------------

    def set_music_volume(self, percent: float) -> bool:
        self._music = max(0.0, min(100.0, float(percent))) / 100.0
        return self._apply_music()

    def music_volume_address(self) -> int | None:
        """[[SOUND_SETTINGS]] + 0x80, guarded by the object's back pointer; the fixed
        3.1.5 address when the chain cannot be followed (None on 3.2)."""
        p = self.game.p
        obj = self.game.deref(p.SOUND_SETTINGS.addr)
        if obj is not None and self.game.read_u32(obj + p.BGM_BACKREF.offset) == p.SOUND_SETTINGS.addr:
            return obj + p.BGM_VOLUME.offset
        return None if p.MUSIC_VOLUME is None else p.MUSIC_VOLUME.addr

    def _apply_music(self) -> bool:
        addr = self.music_volume_address()
        if addr is None or self._music is None:
            self.game.last_error = "music volume: sound object not found"
            return False
        self.game.ov_music.retarget(addr)
        return self.game.ov_music.apply(self.game, struct.pack(">f", self._music))

    def reapply_music(self) -> None:
        if self._music is not None:
            self._apply_music()

    def reapply_sfx(self) -> None:
        if self._sfx is not None:
            self._apply_sfx()

    def set_sfx_volume(self, percent: float) -> bool:
        self._sfx = max(0.0, min(100.0, float(percent))) / 100.0
        return self._apply_sfx()

    def _apply_sfx(self) -> bool:
        p = self.game.p
        return self.game.override(p.SFX_VOLUME, p.SFX_VOLUME.pack(self._sfx))

    # -- shadows (light direction in degrees; reset by the game on stage load) ----------

    def set_shadow_direction(self, x: float | None = None, y: float | None = None) -> bool:
        if x is not None:
            self._shadow_dir[0] = float(x)
        if y is not None:
            self._shadow_dir[1] = float(y)
        return self.apply_shadows()

    def apply_shadows(self) -> bool:
        ok = True
        for field_, value in zip((self.game.p.SHADOW_X, self.game.p.SHADOW_Y), self._shadow_dir, strict=True):
            if value is not None:
                ok = self.game.override(field_, field_.pack(value)) and ok
        return ok

    def set_stage_void_color(self, enabled: bool, rgb: Sequence[float]) -> bool:
        """Void colour: the EFB clear colour, seen around the stage and behind a hidden stage.

        Only RGB has an effect (measured); the alpha written is 255.
        """
        self._background = color_to_bytes(tuple(rgb)[:3]) if enabled else None
        return self.apply_background()

    def apply_background(self) -> bool:
        if self._background is None:
            return self.game.restore_field(self.game.p.BACKGROUND_COLOR)
        return self.game.override(self.game.p.BACKGROUND_COLOR, self._background)
