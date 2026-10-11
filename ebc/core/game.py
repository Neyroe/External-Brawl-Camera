"""The game: connection, address profile, match tracking, and the orchestration of what EBC
changes in it. No ``bpy`` here: vectors are plain tuples.

``Game`` is a ``MemorySession`` (memory.py: the call budget and the tick snapshot) composed of:
``camera`` (camera_writer.py), ``freeze`` (freeze.py), ``roster`` (fighters.py), ``fill``
(fill.py), ``intro`` (intro.py) and ``debug`` (debugmode.py). It keeps the registry of overrides (overrides.py) so that
``restore_all`` puts everything back in one known order, the persistent settings re-applied on a
new match (``reapply_persistent``), the Code Menu display toggles, audio and shadows, and
one-line delegations for what the UI calls. Rules: docs/DEVELOPMENT.md ("Rules", "Adding a
memory address").
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .addresses import (
    DEFAULT_PROFILE,
    PROFILES,
    Field,
    PPlus315,
    detect_profile,
)
from .backend import MemoryBackend
from .camera import CameraPose, CameraSample
from .camera_writer import CameraWriter
from .debugmode import DebugMode
from .display import DisplaySettings
from .fighters import FighterRef, FighterRoster
from .fill import GreenScreen
from .freeze import Freeze
from .intro import IntroSkip
from .memory import MemorySession
from .overrides import BitOverride, Override
from .stages import StageIdentity, identify, match_is_vs

Vec3 = tuple[float, float, float]

# Polls after a new match at which persistent settings are applied again:
# fighters appear during the intro (up to ~90 frames), shadows are reset on
# stage load and the intro lasts ~213 frames.
REAPPLY_DELAYS = (30, 120, 240)
# A decrease of the entry timer only counts as a new match if the new value
# is this small (the u16 wraps after ~18 minutes).
NEW_MATCH_TIMER_MAX = 600
# Polls (counted from the start of a match, or from Connect) at which the stage is identified
# while it is not known yet: a dozen reads each, never once it is known (docs/research/
# stage-identity.md 1.4). From IDENT_ACCEPT_STEX on, the STEX .pac name is taken when the loader
# path cannot be matched to it.
IDENT_POLLS = (0, 2, 6, 15, 45, 120, 240)
IDENT_ACCEPT_STEX = 45


# While the game boots (launcher, before the heaps exist) the frame counter holds heap filler
# (0xCCCCCCCC seen on P+ 3.2): more than 2**31 frames (414 days) is no frame. Same as a menu.
MAX_FRAME = 0x7FFFFFFF


def plausible_frame(value: int) -> int:
    return value if 0 <= value <= MAX_FRAME else 0


_U32 = struct.Struct(">I")
_U16 = struct.Struct(">H")


@dataclass
class PollResult:
    frame: int | None
    new_match: bool = False
    stage_id: int | None = None
    in_match: bool = False
    stage: StageIdentity | None = None  # the stage being played, once identified
    stage_changed: bool = False  # identified (again) on this poll, or left the match


class Game(MemorySession):
    def __init__(self, backend: MemoryBackend, profile: type[PPlus315] = DEFAULT_PROFILE) -> None:
        super().__init__(backend)
        self.p = profile
        # profile: "auto" = detected on connect (see ``connect``), else a PROFILES key
        self.requested_profile = "auto"
        self.profile_info: str | None = None  # what connect() found, for the UI
        self.profile_sure = True  # False: build not recognised, profile guessed
        self._build_overrides()
        # match detection
        self._last_mode: int | None = None
        self._last_timer: int | None = None
        self._pending_match = False
        self._polls_since_match: int | None = None
        self.match_count = 0
        self._stage_present: bool | None = None  # g_Stage valid at the last poll()
        self._poll_frame: int | None = None  # what the last poll() saw (no I/O to ask again)
        self._poll_in_match = False
        # stage identity (stages.identify), refreshed on a new match only
        self.stage: StageIdentity | None = None
        self.match_vs: bool | None = None  # the identified match is a VS match (read with the identity)
        self._ident_polls = 0
        # last known values for the UI (no I/O in draw())
        self.aspect: float | None = None
        self.stage_fov: float | None = None  # stage default FOV, radians
        # components
        self.display = DisplaySettings(self)  # Code Menu toggles, visibility, void colour, audio, shadows
        self.camera = CameraWriter(self)  # camera pose, FOV, roll, near/far, projection
        self.freeze = Freeze(self)  # camera freeze, native or Code Menu
        self.roster = FighterRoster(self)  # fighters, positions, per-fighter mode
        self.fill = GreenScreen(self)  # Full-screen background
        self.intro = IntroSkip(self)  # Skip stage intro camera
        self.debug = DebugMode(self)  # P+ Debug Mode: game pause, frame advance

    def _build_overrides(self) -> None:
        p = self.p
        self.ov: dict[str, Override] = {  # by field name
            f.name: Override(f.addr, f.size)
            for f in (
                p.DEBUG_MENU,
                p.CAM_LOCK,
                p.CAM_LOCK_GAME,
                p.CAM_FOV_COPY,
                p.DRAW_DI,
                p.DISPLAY_HUD,
                p.DISPLAY_HURTBOX,
                p.DISPLAY_STAGE_COLLISION,
                p.SFX_VOLUME,
                p.SHADOW_X,
                p.SHADOW_Y,
                p.BACKGROUND_COLOR,
                p.CAM_ROT_Z,
                p.CAM_FRONT_DEPTH,
                p.CAM_BACK_DEPTH,
            )
        }
        self.ov_stage_fov = Override(0, 4)  # retargeted to [[g_Stage]+0x78]+0x30
        self.ov_music = Override(0, 4)  # retargeted to [[0x90E60F00]]+0x80
        self.bit_stage = BitOverride(p.DISPLAY_STAGE.addr, p.VISIBLE_BIT)
        self.bit_characters = BitOverride(p.DISPLAY_CHAR_MODEL.addr, p.VISIBLE_BIT)
        self.bit_ai = BitOverride(0, p.AI_UPDATE_BIT)  # retargeted to [[0x805A0278]+0x58]+0x94
        self.ov_photo = Override(0, 4)  # retargeted to [[0x805A0278]+0x68]+0x0C (pause camera mode)
        self.bit_debug_pause = BitOverride(p.DEBUG_PAUSE.addr, p.DEBUG_PAUSE_BIT)  # Game.debug

    def set_profile(self, profile: type[PPlus315]) -> None:
        """Switch the address profile. Only while nothing is overridden (connect, offline)."""
        if profile is not self.p:
            self.p = profile
            self._build_overrides()

    def resolve_profile(self) -> type[PPlus315]:
        """Pick the profile for the hooked game: detected, or the one asked for.

        A manual choice wins; ``auto`` falls back to the current profile (and says so
        in ``profile_info``) when the build is not recognised.
        """
        found, info = detect_profile(lambda a, n: self.read_bytes(a, n, fresh=True))
        if self.requested_profile != "auto" and self.requested_profile in PROFILES:
            chosen = PROFILES[self.requested_profile]
            seen = f"detected {found.label}" if found else info
            self.profile_info = f"{chosen.label} (manual; {seen})"
            self.profile_sure = found is None or found is chosen
        elif found is not None:
            chosen = found
            self.profile_info = f"{found.label} ({info}, detected)"
            self.profile_sure = True
        else:
            chosen = self.p
            self.profile_info = f"{chosen.label}? {info}"
            self.profile_sure = False
        self.set_profile(chosen)
        return chosen

    # -- connection ------------------------------------------------------------

    def connect(self) -> bool:
        try:
            ok = bool(self.backend.hook())
        except Exception as exc:  # the backend (DME) may raise anything: not connected  # noqa: BLE001
            self.last_error = str(exc)
            return False
        if ok:
            self._reset_match_tracking()
            self._forget_overrides()
            self.resolve_profile()
            self.read_camera_info()
        return ok

    def disconnect(self) -> bool:
        """Put back every value EBC changed, then release the emulator."""
        ok = self.restore_all() if self.connected else True
        try:
            self.backend.unhook()
        except Exception as exc:  # released anyway: nothing of ours is left to restore  # noqa: BLE001
            self.last_error = f"unhook: {exc}"
        self._reset_match_tracking()
        self._forget_overrides()
        return ok

    def restore_all(self) -> bool:
        """Restore every overridden value, clear tints and show hidden fighters."""
        ok = self.intro.restore()  # code words first: the patch must never stay behind
        ok = self.roster.clear() and ok
        ok = self.fill.release() and ok
        ok = self.restore_projection() and ok  # before the near/far overrides: it holds their value
        for ov in self._all_overrides():
            ok = ov.restore(self) and ok
        return ok

    def _reset_match_tracking(self) -> None:
        self._last_mode = None
        self._last_timer = None
        self._pending_match = False
        self._polls_since_match = None
        self.stage = None
        self.match_vs = None
        self._ident_polls = 0
        self._poll_frame = None
        self._poll_in_match = False

    def _all_overrides(self) -> list[Override | BitOverride]:
        return [
            *self.ov.values(),
            self.ov_stage_fov,
            self.ov_music,
            self.bit_stage,
            self.bit_characters,
            self.bit_ai,
            self.ov_photo,
            self.bit_debug_pause,  # last: the game runs again once everything else is back
        ]

    def _forget_overrides(self) -> None:
        for ov in self._all_overrides():
            ov.forget()
        self.fill.forget()
        self.camera.forget()
        self.roster.forget()
        self.intro.forget()

    def override(self, field: Field, data: bytes, check: bool = True) -> bool:
        return self.ov[field.name].apply(self, data, check)

    def restore_field(self, field: Field) -> bool:
        return self.ov[field.name].restore(self)

    # -- match -------------------------------------------------------------------

    def current_frame(self) -> int | None:
        frame = self.read_value(self.p.MATCH_FRAME)
        return None if frame is None else plausible_frame(frame)

    def emu_tick(self) -> int | None:
        """A counter that moves every frame while Dolphin runs (menus too), frozen in pause."""
        return self.read_value(self.p.EMU_TICK)

    def running_counter(self) -> int | None:
        """Changes every frame while the emulation runs (menus too): the per-frame tick and the match
        frame together, so either one moving means running. None when neither can be read."""
        tick = self.emu_tick()
        frame = self.read_value(self.p.MATCH_FRAME)
        if tick is None and frame is None:
            return None
        return ((tick or 0) << 32) | (frame or 0)

    def game_id(self) -> str | None:
        """Dolphin's game ID (``RSBE01``): names its screenshot folder and savestate files.

        P+ 3.1.5 zeroes the first byte in RAM (``\\0SBE01``): Brawl is RSBE01 whatever it holds.
        """
        data = self.read_bytes(0x80000000, 6)
        if data is None:
            return None
        if data[1:] == b"SBE01":
            return "RSBE01"
        text = data.decode("ascii", "replace")
        return text if text.isalnum() else None

    def stage_id(self) -> int | None:
        return self.read_value(self.p.STAGE_ID)

    def stage_pointer(self) -> int | None:
        return self.deref(self.p.G_STAGE.addr)

    def poll(self) -> PollResult:
        """Call once per sync tick: frame read, match detection, re-apply, stage identity.

        New match = ``REPLAY_MODE`` rising from 0, or the entry timer going
        back to a small value, confirmed once ``g_Stage`` is valid and the
        mode is not 0. ``CAM_TYPE`` and ``MATCH_FRAME == 0`` are not used:
        both also hold in menus / during the intro.
        """
        state = self._read_match_state()
        if state is None:
            return PollResult(frame=None)
        frame, stage_id, mode, timer, in_match = state
        self._detect_new_match(mode, timer)
        self._poll_frame = frame
        self._poll_in_match = in_match
        new_match = self._pending_match and in_match
        if new_match:
            self._on_new_match()
        else:
            self._reapply_after_match()
        stage_changed = self._track_identity(in_match) or new_match
        return PollResult(
            frame=frame,
            new_match=new_match,
            stage_id=stage_id,
            in_match=in_match,
            stage=self.stage,
            stage_changed=stage_changed,
        )

    def _read_match_state(self) -> tuple[int, int, int | None, int | None, bool] | None:
        """(frame, stage id, mode, entry timer, in a match), None when the frame cannot be read.

        Stage id .. frame counter in one read (0x70 bytes), mode .. entry timer in one (0x32).
        """
        p = self.p
        match = self.read_bytes(p.STAGE_ID.addr, p.MATCH_FRAME.addr + 4 - p.STAGE_ID.addr)
        if match is None:
            return None
        frame = plausible_frame(_U32.unpack_from(match, p.MATCH_FRAME.addr - p.STAGE_ID.addr)[0])
        (stage_id,) = _U32.unpack_from(match, 0)
        life = self.read_bytes(p.REPLAY_MODE.addr, p.MATCH_ENTRY_TIMER.addr + 2 - p.REPLAY_MODE.addr)
        mode = None if life is None else _U32.unpack_from(life, 0)[0]
        timer = None if life is None else _U16.unpack_from(life, p.MATCH_ENTRY_TIMER.addr - p.REPLAY_MODE.addr)[0]
        stage = self.stage_pointer()
        self._stage_present = stage is not None
        return frame, stage_id, mode, timer, stage is not None and mode not in (None, 0)

    def _detect_new_match(self, mode: int | None, timer: int | None) -> None:
        """A new match is pending once the mode leaves 0 or the entry timer goes back to a small value."""
        if self._last_mode is not None and mode is not None:
            if self._last_mode == 0 and mode != 0:
                self._pending_match = True
            if (
                timer is not None
                and self._last_timer is not None
                and timer < self._last_timer
                and timer < NEW_MATCH_TIMER_MAX
            ):
                self._pending_match = True
        self._last_mode = mode
        self._last_timer = timer

    def _on_new_match(self) -> None:
        self._pending_match = False
        self.match_count += 1
        self._polls_since_match = 0
        self.roster.forget()  # fighters are recreated: nothing of ours left on them
        self.stage = None  # identified again (reload, savestate of another stage)
        self.match_vs = None
        self._ident_polls = 0
        self.intro.on_new_match()
        self.reapply_persistent()

    def _reapply_after_match(self) -> None:
        """Re-apply at REAPPLY_DELAYS polls after a new match (fighters, shadows come late)."""
        if self._polls_since_match is None:
            return
        self._polls_since_match += 1
        if self._polls_since_match in REAPPLY_DELAYS:
            self.reapply_persistent()
        if self._polls_since_match >= REAPPLY_DELAYS[-1]:
            self._polls_since_match = None

    def _track_identity(self, in_match: bool) -> bool:
        """Identify the stage at IDENT_POLLS while it is not known; forget it out of a match (no I/O).
        True when it was identified (again) or lost on this poll."""
        if in_match:
            if self.stage is not None or self._ident_polls > IDENT_POLLS[-1]:
                return False
            found = False
            if self._ident_polls in IDENT_POLLS:
                self.stage = identify(self, accept_stex=self._ident_polls >= IDENT_ACCEPT_STEX)
                if self.stage is not None:
                    self.match_vs = match_is_vs(self)
                found = self.stage is not None
            self._ident_polls += 1
            return found
        if self.stage is None and not self._ident_polls:
            return False
        lost = self.stage is not None
        self.stage = None  # menus, results: no stage (no I/O)
        self.match_vs = None
        self._ident_polls = 0
        return lost

    def identify_stage(self) -> StageIdentity | None:
        """Identify the stage now (UI button, tools): the same as a new match does."""
        found = identify(self, accept_stex=True)
        if found is not None:
            self.stage = found
            self.match_vs = match_is_vs(self)
        return found

    # -- savestates ---------------------------------------------------------------------
    #
    # A savestate keeps the RAM: nothing of ours that the game must not keep may travel in it
    # (the orthographic projection, the intro code patch). After a load the state brought its
    # own RAM back: Code Menu values, colours, the camera controller.

    def before_savestate(self) -> bool:
        """Before a save OR a load: the game's perspective back (the next sync tick sets ours again)."""
        return self.restore_projection()

    def save_state(self, save: Callable[[], bool]) -> bool:
        """Run ``save`` (the emulator's Save State) with the intro patch removed around it.

        The patch is put back even when ``save`` raises.
        """
        self.intro.before_save()
        try:
            return save()
        finally:
            self.intro.after_save()

    def after_state_load(self) -> None:
        """A state was loaded: what the intro skip knew is gone, every setting is written again."""
        self.intro.after_load()
        self.reapply_persistent()

    def hold_intro_skip(self, pose: CameraPose | None) -> bool:
        """Per tick, before the camera write: Skip stage intro camera (no I/O when off / fighting)."""
        return self.intro.tick(self._poll_frame, self._poll_in_match, pose)

    def set_intro_skip(self, enabled: bool) -> bool:
        return self.intro.set_enabled(enabled)

    def persistent_steps(self) -> tuple[tuple[str, Callable[[], object]], ...]:
        """What ``reapply_persistent`` writes again, in this order. Each step is a no-op (no I/O) for a
        setting never set. The order is measured behaviour: the freeze after the Code Menu toggles
        (the lock copy follows the lock), the fighters after the stage and the fill, the intro
        skip's code words last."""
        d = self.display
        return (
            ("camera info", self.read_camera_info),  # aspect and stage FOV shown by the UI
            ("music", d.reapply_music),
            ("sound effects", d.reapply_sfx),
            ("shadows", d.apply_shadows),
            ("Code Menu toggles", d.reapply_flags),
            ("camera freeze", self.freeze.reapply),  # lock copy, native bit (a new match rebuilds it)
            ("characters", d.reapply_characters),
            ("stage", d.apply_stage),
            ("void colour", d.apply_background),
            ("full-screen background", self.fill.reapply),
            ("near / far clip", self.camera.reapply_depth),
            ("fighters", self.refresh_fighters),
            ("intro skip", self.intro.check),  # one read when the code words are right
        )

    def reapply_persistent(self) -> None:
        """Write again every setting the user changed (new match, savestate load, Re-apply)."""
        for _name, step in self.persistent_steps():
            step()

    def read_camera_info(self) -> None:
        """Refresh the aspect and the stage default FOV shown by the UI."""
        aspect = self.read_value(self.p.CAM_ASPECT)
        if aspect is not None and math.isfinite(aspect) and 0.5 < aspect < 4.0:
            self.aspect = float(aspect)
        fov = self.camera.stage_default_fov()
        if fov is not None:
            self.stage_fov = fov

    # -- camera (camera_writer.py) -------------------------------------------------------

    def write_camera_pose(self, pose: CameraPose, cut: bool = False, suppress_shake: bool = False) -> bool:
        return self.camera.write_pose(pose, cut, suppress_shake)

    def read_camera_sample(self, frame: int | None = None) -> CameraSample | None:
        return self.camera.read_sample(frame)

    def restore_projection(self) -> bool:
        return self.camera.restore_projection()

    @property
    def ortho_active(self) -> bool:
        return self.camera.ortho_active

    def suppress_screen_shake(self) -> bool:
        return self.camera.suppress_screen_shake()

    def clear_fov(self) -> bool:
        return self.camera.clear_fov()

    def clear_camera_roll(self) -> bool:
        return self.camera.clear_roll()

    def set_front_depth(self, value: float) -> bool:
        return self.camera.set_front_depth(value)

    def set_back_depth(self, value: float) -> bool:
        return self.camera.set_back_depth(value)

    # -- fighters (fighters.py) --------------------------------------------------------

    def fighters(self) -> dict[int, FighterRef]:
        return self.roster.resolve()

    def player_position(self, port: int, refs: dict[int, FighterRef] | None = None) -> Vec3 | None:
        return self.roster.position(port, refs)

    def set_fighter_mode(self, port: int, mode: str, rgba: Sequence[float] = (0.0, 0.0, 0.0, 0.0)) -> bool:
        return self.roster.set_mode(port, mode, rgba)

    def needs_fighter_refresh(self) -> bool:
        return self.roster.needs_refresh()

    def refresh_fighters(self, refs: dict[int, FighterRef] | None = None) -> int:
        return self.roster.refresh(refs)

    # -- display (display.py) ---------------------------------------------------------------

    def set_flag(self, field: Field, value: int | bool) -> bool:
        return self.display.set_flag(field, value)

    def set_hud(self, enabled: bool) -> bool:
        return self.display.set_hud(enabled)

    def set_debug_menu(self, enabled: bool) -> bool:
        return self.debug.set_enabled(enabled)

    def set_draw_di(self, enabled: bool) -> bool:
        return self.display.set_draw_di(enabled)

    def set_characters_display(self, mode: str) -> bool:
        return self.display.set_characters_display(mode)

    def assert_display(self) -> bool:
        return self.display.assert_display()

    def set_stage_display(self, mode: str) -> bool:
        return self.display.set_stage_display(mode)

    def set_stage_void_color(self, enabled: bool, rgb: Sequence[float]) -> bool:
        return self.display.set_stage_void_color(enabled, rgb)

    def set_music_volume(self, percent: float) -> bool:
        return self.display.set_music_volume(percent)

    def set_sfx_volume(self, percent: float) -> bool:
        return self.display.set_sfx_volume(percent)

    def set_shadow_direction(self, x: float | None = None, y: float | None = None) -> bool:
        return self.display.set_shadow_direction(x, y)

    # -- camera freeze (freeze.py: the ONE entry point) ---------------------------------

    def freeze_camera(self, frozen: bool) -> bool:
        return self.freeze.set(frozen)

    def hold_freeze(self) -> bool:
        return self.freeze.hold()

    def set_freeze_method(self, method: str) -> bool:
        return self.freeze.set_method(method)

    @property
    def freeze_method(self) -> str:
        return self.freeze.method

    @freeze_method.setter
    def freeze_method(self, method: str) -> None:
        self.freeze.method = method

    @property
    def native_freeze(self) -> bool:
        return self.freeze.native

    # -- full-screen background (fill.py) ------------------------------------------------

    def set_green_screen(self, enabled: bool, rgba: Sequence[float]) -> bool:
        return self.fill.set(enabled, rgba)

    def hold_green_screen(self) -> bool:
        return self.fill.hold(self._stage_present)

    @property
    def green_screen(self) -> bool:
        return self.fill.enabled
