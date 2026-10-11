"""The camera freeze: the ONE entry point that writes the camera lock or the update bit. No ``bpy``.

Everything else (UI, sync, cuts, hand back, the camera writer) calls ``Game.freeze`` only
(docs/research/camera-freeze.md). Two mechanisms:

* ``FREEZE_NATIVE`` (default, recipe R2): clear bit 0x10 of cmAIController+0x94. The match camera,
  its quake and the subject camera stop, and nobody calls gfCamera::update: the camera writer then
  writes the whole derived gfCamera block in one write. Exact from the next frame, free target Z, no
  shake, back to our camera after a pause, no Code Menu. Releasing it = the game glides from our
  camera (Hand back). The bit is lost with a new controller: held every tick.
* ``FREEZE_CODE_MENU`` (legacy): the P+ Code Menu lock and its game copy.

Either way, the pause camera (cmPhotoController, kind 7) is stopped too while frozen
(docs/research/pause-camera.md): its mode word is set to a value its update ignores, so nobody
writes gfCamera in pause and the camera writer sends the whole block there as well. Held in the
match too, so the very first pause frame is already ours; given back with the freeze.

``run_match_camera`` is the one other writer of the update bit (Skip stage intro camera while EBC
does not drive): it sets the bit without taking it over.
"""

from __future__ import annotations

import struct
from enum import StrEnum
from typing import TYPE_CHECKING

from .overrides import set_bit

_U32 = struct.Struct(">I")

if TYPE_CHECKING:
    from .game import Game


class FreezeMethod(StrEnum):
    NATIVE = "NATIVE"  # cmAIController update bit + EBC writes the whole gfCamera block
    CODE_MENU = "CODE_MENU"  # P+ Code Menu camera lock (legacy)


FREEZE_NATIVE = FreezeMethod.NATIVE
FREEZE_CODE_MENU = FreezeMethod.CODE_MENU
FREEZE_METHODS = tuple(FreezeMethod)


class Freeze:
    """``Game.freeze``. Its overrides (``bit_ai``, the Code Menu lock and its copy) live in the
    Game's registry, so ``restore_all`` puts them back with the others."""

    def __init__(self, game: Game) -> None:
        self.game = game
        self.method = FREEZE_NATIVE
        self._frozen = False  # set() state, whatever the method
        self.lock: bool | None = None  # Code Menu lock wanted (legacy freeze), None = never set
        self._held_tick: int | None = None  # tick_serial of the tick the native freeze was checked in
        self._photo_tick: int | None = None  # same, for the pause camera

    def set_method(self, method: str) -> bool:
        """Switch mechanism; a freeze in place is moved to the new one."""
        if method not in FREEZE_METHODS:
            raise ValueError(f"unknown freeze method {method!r}")
        if method == self.method:
            return True
        frozen = self._frozen
        ok = self.set(False) if frozen else True
        self.method = method
        return (self.set(True) if frozen else True) and ok

    def set(self, frozen: bool) -> bool:
        """Freeze the game camera (what EBC writes stays) or give it back to the game.

        Restored by ``restore_all``.
        """
        self._frozen = bool(frozen)
        if self.method == FREEZE_NATIVE:
            ok = self.hold_native() if frozen else self._release_native()
        else:
            ok = self.set_lock(frozen)
        if frozen:
            self.hold_photo()  # best effort: no photo controller in some scenes, the writer checks again
        else:
            ok = self._release_photo() and ok
        return self.game.intro.on_freeze_change() and ok

    def reassert(self) -> bool:
        """Before a cut: make sure the freeze holds from the very next frame."""
        ok = self.assert_lock()  # the Code Menu lock, if one is set (legacy, tools)
        if self.method == FREEZE_NATIVE and self._frozen:
            ok = self.hold_native() and ok
        if self._frozen:
            self.hold_photo()  # best effort, as in set()
        return ok

    def hold(self) -> bool:
        """Per tick: keep the native freeze (a new match or a savestate brings the bit back) and the
        pause camera stopped (a savestate may bring its mode back).

        Free when it holds: the flags byte and the mode word are in the tick snapshot (next to the
        controller pointers this chain reads anyway), nothing is written. Held in the match too, so
        the first pause frame is already ours. Code Menu method: the camera writer holds the pause
        camera only in pause (a match tick reads no CameraController pointer there).
        """
        if self.method != FREEZE_NATIVE or not self._frozen:
            return True
        self.hold_photo()  # best effort: the camera writer checks it again before a pause write
        return self.hold_native()

    def reapply(self) -> None:
        """New match / Re-apply: the Code Menu lock copy if one is wanted, then the native bit (a new
        match rebuilds the camera controller)."""
        if self.lock is not None:
            self._apply_lock_copy()
        self.hold()

    @property
    def frozen(self) -> bool:
        return bool(self._frozen)

    @property
    def native(self) -> bool:
        """Frozen with the native mechanism: the target may leave the stage plane."""
        return self._frozen and self.method == FREEZE_NATIVE

    # -- native: cmAIController update bit ---------------------------------------------------

    def ai_flags_address(self) -> int | None:
        """cmAIController+0x94 of the match camera, None unless it drives gfCamera (the guard)."""
        game, p = self.game, self.game.p
        cc = game.deref(p.CAMERA_CONTROLLER.addr)
        if cc is None:
            return None
        ai = game.deref(cc + p.CC_AI_CONTROLLER.offset)
        if ai is None or game.read_u32(ai + p.AI_CAMERA.offset) != p.CAM_RECORD_BASE:
            return None  # not the match controller we know: write nothing
        return ai + p.AI_FLAGS.offset

    def hold_native(self) -> bool:
        """Clear the update bit (original remembered). True when the freeze holds."""
        game = self.game
        if self._held_tick == game.tick_serial and game.in_tick:
            return True  # already checked in this tick
        addr = self.ai_flags_address()
        if addr is None:
            return False
        bit = game.bit_ai
        bit.retarget(addr)
        current = game.read_bytes(addr, 1)  # tick snapshot: already clear = nothing to do
        if current is not None and bit.active and not bit.is_on(current[0]):
            held = True
        else:
            # During the stage intro the game itself clears the bit (intro-camera.md 1.3): the value to
            # give back is the match value, set, not the cleared bit found now.
            original = True if not bit.active and self._in_intro() else None
            held = bit.apply(game, False, original)
        self._held_tick = game.tick_serial if held and game.in_tick else None
        return held

    def _in_intro(self) -> bool:
        """Stage intro: a match is loaded and the frame counter has not started (one read)."""
        frame = self.game.current_frame()
        return frame == 0 and self.game.stage_pointer() is not None

    def _release_native(self) -> bool:
        self._held_tick = None
        bit = self.game.bit_ai
        if not bit.active:
            return True
        if self.ai_flags_address() != bit.addr:
            bit.forget()  # the controller is gone: nothing of ours left
            return True
        return bit.restore(self.game)

    # -- pause camera: the cmPhotoController mode word (pause-camera.md 2) ---------------------

    def photo_mode_address(self) -> int | None:
        """cmPhotoController+0x0C, None unless it is the pause controller we know driving gfCamera."""
        game, p = self.game, self.game.p
        cc = game.deref(p.CAMERA_CONTROLLER.addr)
        if cc is None:
            return None
        photo = game.deref(cc + p.CC_PHOTO_CONTROLLER.offset)
        if photo is None or game.read_u32(photo + p.PHOTO_CAMERA.offset) != p.CAM_RECORD_BASE:
            return None
        if game.read_u32(photo + 4) != p.PHOTO_VTABLE:
            return None  # not a cmPhotoController: write nothing
        return photo + p.PHOTO_MODE.offset

    def hold_photo(self) -> bool:
        """Stop the pause camera's update (original mode remembered). True when it holds.

        Nothing is written when the mode word (tick snapshot) already holds ours. A mode found
        already stopped (a savestate made while EBC held it) is remembered as the game's 0.
        """
        game, p = self.game, self.game.p
        if self._photo_tick == game.tick_serial and game.in_tick:
            return True
        addr = self.photo_mode_address()
        if addr is None:
            return False
        ov = game.ov_photo
        ov.retarget(addr)
        frozen = _U32.pack(p.PHOTO_MODE_FROZEN)
        # ours already (tick snapshot): no write, in a tick or not
        already = ov.active and game.read_bytes(addr, 4) == frozen
        held = already or ov.apply(game, frozen, skip_same=True)
        if ov.original == frozen:
            ov.original = _U32.pack(0)  # the constructor's value: the photo camera runs
        self._photo_tick = game.tick_serial if held and game.in_tick else None
        return held

    def _release_photo(self) -> bool:
        """Give the pause camera back. In pause it continues from our camera (camera writer)."""
        self._photo_tick = None
        ov = self.game.ov_photo
        if not ov.active:
            return True
        if self.photo_mode_address() != ov.addr:
            ov.forget()
            return True
        paused = self.game.camera.is_paused()
        if not ov.restore(self.game):
            return False
        return self.game.camera.seed_pause_camera() if paused else True

    @property
    def photo_held(self) -> bool:
        """The pause camera is stopped by EBC (no I/O)."""
        return self.game.ov_photo.active

    def run_match_camera(self) -> bool:
        """Set the update bit WITHOUT taking it over (no original kept, nothing restored): the game's
        own match camera runs. Only for Skip stage intro camera when EBC does not drive the camera
        (the intro operator clears the bit, intro-camera.md 2)."""
        addr = self.ai_flags_address()
        return addr is not None and set_bit(self.game, addr, self.game.p.AI_UPDATE_BIT, True)

    # -- legacy: the Code Menu lock ----------------------------------------------------------

    def set_lock(self, enabled: bool) -> bool:
        """The Code Menu mechanism: Code Menu lock AND the game's own copy (u16 0x80583FFA).

        P+ copies the menu value once per frame, too late for the current one:
        writing the copy too makes the lock (or its release) effective on the
        next frame. Both are restored by ``restore_all``.
        """
        self.lock = bool(enabled)
        ok = self.game.set_flag(self.game.p.CAM_LOCK, enabled)
        return self._apply_lock_copy() and ok

    def _apply_lock_copy(self) -> bool:
        p = self.game.p
        return self.game.override(p.CAM_LOCK_GAME, p.CAM_LOCK_GAME.pack(1 if self.lock else 0))

    def assert_lock(self) -> bool:
        """Write the lock copy again if the lock is wanted (before a cut)."""
        if not self.lock:
            return True
        p = self.game.p
        return self.game.override(p.CAM_LOCK_GAME, p.CAM_LOCK_GAME.pack(1), check=False)
