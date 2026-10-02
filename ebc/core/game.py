"""High level game API. No ``bpy`` here: vectors are plain tuples.

Rules (see docs/V3_PLAN.md and docs/research/addresses-pplus-3.1.5.md):

* nothing is written while the backend is not hooked: every setter is a
  no-op that returns ``False``, it never raises;
* every value EBC overrides keeps the game's original, restored when the
  option is turned off and by ``restore_all()`` (called by ``disconnect()``);
* settings the game resets (shadows on stage load, music on the CSS) or that
  must survive a new match are remembered and re-applied by ``poll()`` when a
  new match is detected;
* fighter tints and hides are re-asserted on every sync tick
  (``refresh_fighters``): a KO/respawn resets both channels.

Coordinate systems: Blender is Z-up, Brawl is Y-up with X mirrored. The
conversion is its own inverse: ``(x, y, z) <-> (-x, z, y)``.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from .addresses import (
    DEFAULT_PROFILE,
    MEM1_END,
    MEM1_START,
    MEM2_END,
    MEM2_START,
    PROFILES,
    Field,
    PPlus315,
    detect_profile,
    is_valid_pointer,
)
from .backend import BackendError, MemoryBackend
from .camera import (
    ORTHO_FAR_MARGIN,
    ORTHO_NEAR,
    PROJECTION_ORTHOGRAPHIC,
    PROJECTION_PERSPECTIVE,
    CameraPose,
    CameraSample,
    pack_gfcamera_block,
    parse_camera_block,
    resolve_ortho,
    valid_aspect,
)
from .camera import camera_roll as camera_roll  # re-exported (UI, tests)

Vec3 = tuple[float, float, float]
RGBA = tuple[float, float, float, float]

# Far clip at or above this value is sent as "infinite".
BACK_DEPTH_INFINITE_THRESHOLD = 4999
BACK_DEPTH_INFINITE = 1_000_000.0

# Measured default (vertical, all stages tested): 0.5236 rad.
GAME_DEFAULT_FOV_DEG = 30.0
GAME_DEFAULT_ASPECT = 1.7323

# The game's own projection words in a match (ortho-projection.md 1.2): restored when the ones
# found at the first orthographic write are not a perspective camera (a state saved in ortho).
DEFAULT_NEAR_FAR = (1.0, 1_000_000.0)
DEFAULT_ORTHO_BOUNDS = (0.0, 480.0, 0.0, 640.0)

# -- Unit conversions -----------------------------------------------------------


def blender_to_brawl(v: Sequence[float]) -> Vec3:
    return (-float(v[0]), float(v[2]), float(v[1]))


def brawl_to_blender(v: Sequence[float]) -> Vec3:
    return (-float(v[0]), float(v[2]), float(v[1]))


def fov_to_game(vertical_fov_rad: float) -> float:
    """The game takes the full VERTICAL FOV in radians (measured)."""
    return float(vertical_fov_rad)


def fov_from_game(value: float) -> float:
    return float(value)


def color_to_bytes(rgba: Sequence[float]) -> bytes:
    """Float colour (0..1, already gamma-encoded / sRGB) -> RGBA bytes."""
    vals = list(rgba) + [1.0] * (4 - len(rgba))
    return bytes(max(0, min(255, round(float(c) * 255))) for c in vals[:4])


def look_angles(target: Sequence[float], eye: Sequence[float]) -> tuple[float, float, float]:
    """Brawl-space target/eye -> (pitch, yaw, distance), the game's convention.

    Checked on the in-game camera: eye (0, 55.11, 154.67), target (0, 35.57, 0)
    gives the game's own rotation X of -0.1257.
    """
    dx, dy, dz = (float(e) - float(t) for e, t in zip(eye, target, strict=True))
    horizontal = math.hypot(dx, dz)
    return -math.atan2(dy, horizontal), math.atan2(dx, dz), math.sqrt(dx * dx + dy * dy + dz * dz)


# -- Enumerations shared with the UI ------------------------------------------

CHARACTERS_MODEL = "MODEL"
CHARACTERS_MODEL_HURTBOXES = "MODEL_HURTBOXES"
CHARACTERS_HURTBOXES = "HURTBOXES"
CHARACTERS_HIDDEN = "HIDDEN"

STAGE_VISIBLE = "VISIBLE"
STAGE_VISIBLE_COLLISION = "VISIBLE_COLLISION"
STAGE_COLLISION_ONLY = "COLLISION_ONLY"
STAGE_HIDDEN = "HIDDEN"

FIGHTER_NORMAL = "NORMAL"  # untouched
FIGHTER_COLOUR = "COLOUR"  # sub-colour tint (colour + alpha)
FIGHTER_HIDDEN = "HIDDEN"  # not drawn (soVisibilityModule)
FIGHTER_MODES = (FIGHTER_NORMAL, FIGHTER_COLOUR, FIGHTER_HIDDEN)

FREEZE_NATIVE = "NATIVE"  # cmAIController update bit + EBC writes the whole gfCamera block
FREEZE_CODE_MENU = "CODE_MENU"  # P+ Code Menu camera lock (legacy)
FREEZE_METHODS = (FREEZE_NATIVE, FREEZE_CODE_MENU)

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

_CLEAR = b"\x00\x00\x00\x00"
# efScreen request 9 as EBC writes it (green-screen-alpha.md 2): +0x01 active, +0x03 layer 0,
# +0x04 priority FF (above a Final Smash dimming), +0x05 type 2 (flat fill), +0x08 auto-end 0
# (held for ever), duration and elapsed 0 (colour at once); then 3 x RGBA (start, target, current).
_FILL_HEAD = bytes([0x00, 0x01, 0x00, 0x00, 0xFF, 0x02, 0x00, 0x00]) + bytes(12)
_FREE_SLOT = 0xFF

# Polls after a new match at which persistent settings are applied again:
# fighters appear during the intro (up to ~90 frames), shadows are reset on
# stage load and the intro lasts ~213 frames.
REAPPLY_DELAYS = (30, 120, 240)
# A decrease of the entry timer only counts as a new match if the new value
# is this small (the u16 wraps after ~18 minutes).
NEW_MATCH_TIMER_MAX = 600


# -- Memory access budget (docs/DEVELOPMENT.md "Memory access budget") ----------
#
# Measured against a real Dolphin (dolphin-memory-engine 1.3.1, Linux): one call costs
# ~2.8 us whatever its size up to 16 KiB (4 B: 2.75 us, 4 KiB: 2.96 us, 16 KiB: 3.13 us,
# 64 KiB: 11 us). Two reads closer than MERGE_GAP are therefore cheaper as one.
MERGE_GAP = 4096
MAX_SPAN = 16384


# While the game boots (launcher, before the heaps exist) the frame counter holds heap filler
# (0xCCCCCCCC seen on P+ 3.2): more than 2**31 frames (414 days) is no frame. Same as a menu.
MAX_FRAME = 0x7FFFFFFF


def plausible_frame(value: int) -> int:
    return value if 0 <= value <= MAX_FRAME else 0


@dataclass
class IOStats:
    """Backend calls made by the owner thread (the one that created the Game)."""

    reads: int = 0
    writes: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    skipped: int = 0  # writes not sent: this tick's snapshot already held exactly those bytes
    hits: int = 0  # reads served by this tick's snapshot
    ns: int = 0  # time spent inside backend calls

    @property
    def calls(self) -> int:
        return self.reads + self.writes

    def copy(self) -> IOStats:
        return IOStats(self.reads, self.writes, self.bytes_read, self.bytes_written, self.skipped, self.hits, self.ns)

    def since(self, before: IOStats) -> IOStats:
        return IOStats(
            self.reads - before.reads,
            self.writes - before.writes,
            self.bytes_read - before.bytes_read,
            self.bytes_written - before.bytes_written,
            self.skipped - before.skipped,
            self.hits - before.hits,
            self.ns - before.ns,
        )


def _region(addr: int) -> int | None:
    if MEM1_START <= addr < MEM1_END:
        return 1
    if MEM2_START <= addr < MEM2_END:
        return 2
    return None


def plan_reads(ranges: Sequence[tuple[int, int]], gap: int = MERGE_GAP, span: int = MAX_SPAN) -> list[tuple[int, int]]:
    """Merge ``(addr, size)`` ranges into as few reads as possible.

    Two ranges are read together when the hole between them is at most ``gap`` bytes and the
    whole read stays within ``span`` bytes and one memory region (MEM1 or MEM2): only bytes the
    game owns are read, nothing is written. Ranges outside MEM1/MEM2 are dropped.
    """
    clean = sorted(
        (a, a + n) for a, n in set(ranges) if n > 0 and _region(a) is not None and _region(a) == _region(a + n - 1)
    )
    out: list[list[int]] = []
    for start, end in clean:
        if out:
            cur = out[-1]
            if start - cur[1] <= gap and max(end, cur[1]) - cur[0] <= span and _region(start) == _region(cur[0]):
                cur[1] = max(cur[1], end)
                continue
        out.append([start, end])
    return [(a, b - a) for a, b in out]


_GROUPS: dict[tuple, tuple[int, int, list]] = {}  # read_group layouts
_U32 = struct.Struct(">I")
_U16 = struct.Struct(">H")


class _Snapshot:
    """Bytes read during one tick (memory as it was then), kept up to date with our own writes."""

    def __init__(self) -> None:
        self.blocks: list[tuple[int, bytearray]] = []

    def get(self, addr: int, n: int) -> bytes | None:
        for start, buf in self.blocks:
            off = addr - start
            if off >= 0 and off + n <= len(buf):
                return bytes(buf[off : off + n])
        return None

    def patch(self, addr: int, data: bytes) -> None:
        """Newer bytes (our write, a fresh read): update every copy of them."""
        end = addr + len(data)
        for start, buf in self.blocks:
            lo, hi = max(addr, start), min(end, start + len(buf))
            if lo < hi:
                buf[lo - start : hi - start] = data[lo - addr : hi - addr]

    def put(self, addr: int, data: bytes) -> None:
        self.patch(addr, data)
        self.blocks.append((addr, bytearray(data)))


@dataclass
class PollResult:
    frame: int | None
    new_match: bool = False
    stage_id: int | None = None
    in_match: bool = False


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


class _Override:
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

    def apply(self, game: Game, data: bytes, check: bool = True, skip_same: bool = False) -> bool:
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

    def restore(self, game: Game) -> bool:
        if self.original is None:
            return True
        if not game.write_bytes(self.addr, self.original):
            return False
        self.forget()
        return True

    def forget(self) -> None:
        self.original = None
        self.written = None


class _BitOverride:
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

    def capture(self, game: Game) -> bool:
        """Remember the original bit now (before something else changes it)."""
        if self.original is None:
            data = game.read_bytes(self.addr, 1, fresh=True)
            if data is None:
                return False
            self.original = bool(data[0] & self.bit)
        return True

    def _write(self, game: Game, on: bool) -> bool:
        # read-modify-write of a byte the game also owns: never from the tick snapshot
        data = game.read_bytes(self.addr, 1, fresh=True)
        if data is None:
            return False
        current = data[0]
        if self.original is None:
            self.original = bool(current & self.bit)
        new = current | self.bit if on else current & ~self.bit & 0xFF
        return new == current or game.write_bytes(self.addr, bytes([new]))

    def apply(self, game: Game, on: bool) -> bool:
        return self._write(game, on)

    def restore(self, game: Game) -> bool:
        if self.original is None:
            return True
        if not self._write(game, self.original):
            return False
        self.original = None
        return True

    def forget(self) -> None:
        self.original = None


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


class Game:
    def __init__(self, backend: MemoryBackend, profile: type[PPlus315] = DEFAULT_PROFILE) -> None:
        self.backend = backend
        self.p = profile
        self.last_error: str | None = None
        # match detection
        self._last_mode: int | None = None
        self._last_timer: int | None = None
        self._pending_match = False
        self._polls_since_match: int | None = None
        self.match_count = 0
        # last known values for the UI (no I/O in draw())
        self.aspect: float | None = None
        self.stage_fov: float | None = None  # stage default FOV, radians
        # persistent desired state (None = never set by the user)
        self._music: float | None = None
        self._sfx: float | None = None
        self._shadow_dir: list[float | None] = [None, None]
        self._flags: dict[str, int] = {}
        self._characters: str | None = None
        self._stage: str | None = None
        self._near: float | None = None
        self._lock: bool | None = None  # Code Menu lock (legacy freeze)
        self._frozen = False  # freeze_camera() state, whatever the method
        self.freeze_method = FREEZE_NATIVE
        self._far: float | None = None
        self._fill: bytes | None = None  # full-screen fill colour (efScreen layer 0), None = off
        self._fill_base: int | None = None  # efScreen the original of request 9 was read from
        self._fill_original: bytes | None = None  # request 9 as the game had it
        self._background: bytes | None = None  # void colour (EFB clear colour)
        # projection words as the game had them before the first orthographic write: near/far (8 B),
        # bounds (16 B), type (4 B); None = perspective, nothing of ours in place
        self._projection: tuple[bytes, bytes, bytes] | None = None
        self._stage_present: bool | None = None  # g_Stage valid at the last poll()
        self._fighter_modes: dict[int, _FighterMode] = {}
        # fighter channels we wrote: port -> {module address: soModuleAccesser}
        self._painted: dict[int, dict[int, int]] = {}
        self._hidden: dict[int, dict[int, int]] = {}
        # profile: "auto" = detected on connect (see ``connect``), else a PROFILES key
        self.requested_profile = "auto"
        self.profile_info: str | None = None  # what connect() found, for the UI
        self.profile_sure = True  # False: build not recognised, profile guessed
        self._build_overrides()
        # memory access budget: call counters, per-tick snapshot (see ``tick``)
        self._owner = threading.get_ident()
        self.io = IOStats()
        self._snap: _Snapshot | None = None
        self._read_set: list[tuple[int, int]] = []  # ranges read in the current tick
        self._prefetch_set: list[tuple[int, int]] = []  # ... in the previous one: read first, merged
        self._wanted: dict[int, bytes] = {}  # skip_same writes asked in the current tick
        self._last_wanted: dict[int, bytes] = {}
        self._plan: tuple[frozenset, list[tuple[int, int]]] = (frozenset(), [])
        self._held_in_tick = False  # native freeze checked in this tick
        self._tick_hooked = False  # backend hooked when the tick opened, and no call failed since
        self._block_cache: tuple[tuple, bytes] | None = None  # (inputs, gfCamera block) of the last tick

    def _build_overrides(self) -> None:
        p = self.p
        self._ov: dict[str, _Override] = {
            f.name: _Override(f.addr, f.size)
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
        self._ov_stage_fov = _Override(0, 4)  # retargeted to [[g_Stage]+0x78]+0x30
        self._ov_music = _Override(0, 4)  # retargeted to [[0x90E60F00]]+0x80
        self._bit_stage = _BitOverride(p.DISPLAY_STAGE.addr, p.VISIBLE_BIT)
        self._bit_characters = _BitOverride(p.DISPLAY_CHAR_MODEL.addr, p.VISIBLE_BIT)
        self._bit_ai = _BitOverride(0, p.AI_UPDATE_BIT)  # retargeted to [[0x805A0278]+0x58]+0x94

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

    @property
    def connected(self) -> bool:
        if self._tick_hooked and self._snap is not None and threading.get_ident() == self._owner:
            return True  # checked when the tick opened; a failed call ends it (_tick_hooked)
        try:
            return bool(self.backend.is_hooked())
        except Exception:
            return False

    def connect(self) -> bool:
        try:
            ok = bool(self.backend.hook())
        except Exception as exc:
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
        except Exception:
            pass
        self._reset_match_tracking()
        self._forget_overrides()
        return ok

    def restore_all(self) -> bool:
        """Restore every overridden value, clear tints and show hidden fighters."""
        ok = self._clear_fighters()
        ok = self._release_fill() and ok
        ok = self.restore_projection() and ok  # before the near/far overrides: it holds their value
        for ov in self._all_overrides():
            ok = ov.restore(self) and ok
        return ok

    def _reset_match_tracking(self) -> None:
        self._last_mode = None
        self._last_timer = None
        self._pending_match = False
        self._polls_since_match = None

    def _all_overrides(self) -> list[_Override | _BitOverride]:
        return [
            *self._ov.values(),
            self._ov_stage_fov,
            self._ov_music,
            self._bit_stage,
            self._bit_characters,
            self._bit_ai,
        ]

    def _forget_overrides(self) -> None:
        for ov in self._all_overrides():
            ov.forget()
        self._fill_base = None
        self._fill_original = None
        self._projection = None
        self._painted.clear()
        self._hidden.clear()

    # -- tick snapshot ----------------------------------------------------------------
    #
    # Inside ``with game.tick():`` (the memory phase of one sync tick, owner thread only):
    #
    # * the ranges read by the previous tick are read again first, merged by ``plan_reads``
    #   (pointer chains rarely move: their next hops are then already in the snapshot; a hop
    #   that moved is a miss, read on its own). Every pointer is still checked by the code
    #   that follows it: the snapshot holds real bytes of this tick, nothing is guessed;
    # * reads are served from the snapshot when it covers them;
    # * ``write_bytes(..., skip_same=True)`` sends nothing when the snapshot already holds
    #   exactly those bytes (the game has them now: writing them again changes nothing).
    #   The range of a write repeated with the same bytes is read with the next snapshot,
    #   so a still camera / a kept tint costs no write at all, a moving one no extra read.
    #
    # Outside a tick (UI callbacks, the recorder thread, tools) every access goes straight
    # to the backend, as before.

    @contextmanager
    def tick(self) -> Iterator[IOStats]:
        """One sync tick's memory phase: snapshot reads, skipped writes. Not re-entrant."""
        if self._snap is not None or threading.get_ident() != self._owner:
            yield self.io
            return
        self._snap = _Snapshot()
        self._read_set = []
        self._wanted = {}
        self._held_in_tick = False
        self._tick_hooked = False
        try:
            self._tick_hooked = self.connected
            if self._tick_hooked:
                key = frozenset(self._prefetch_set)
                if self._plan[0] != key:  # the same ranges tick after tick: plan once
                    self._plan = (key, plan_reads(self._prefetch_set))
                for addr, n in self._plan[1]:
                    data = self._backend_read(addr, n)
                    if data is None:
                        break  # disconnected or unreadable: the code below reads what it needs
                    self._snap.put(addr, data)
            yield self.io
        finally:
            self._prefetch_set = list(set(self._read_set))
            self._tick_hooked = False
            self._last_wanted = self._wanted
            self._snap = None
            self._read_set = []
            self._wanted = {}

    def _tick_snapshot(self) -> _Snapshot | None:
        if self._snap is None or threading.get_ident() != self._owner:
            return None
        return self._snap

    # -- raw access (never raises) -----------------------------------------------

    def _count(self, t0: int, reads: int, writes: int, nbytes: int) -> None:
        if threading.get_ident() != self._owner:
            return  # the recorder thread: not part of the sync budget
        io = self.io
        io.ns += time.perf_counter_ns() - t0
        io.reads += reads
        io.writes += writes
        if reads:
            io.bytes_read += nbytes
        else:
            io.bytes_written += nbytes

    def _backend_read(self, addr: int, n: int) -> bytes | None:
        t0 = time.perf_counter_ns()
        try:
            return self.backend.read(addr, n)
        except BackendError as exc:
            self.last_error = str(exc)
        except Exception as exc:
            self.last_error = f"unexpected backend error: {exc}"
        finally:
            self._count(t0, 1, 0, n)
        self._tick_hooked = False  # the backend may have unhooked: ask it again
        return None

    def read_bytes(self, addr: int, n: int, fresh: bool = False) -> bytes | None:
        """``n`` bytes at ``addr``, None if unreadable.

        In a tick, served from the snapshot unless ``fresh`` (read-modify-write of a byte the
        game may change at any time).
        """
        snap = self._snap
        if snap is not None and self._tick_hooked and threading.get_ident() == self._owner:
            if not fresh:  # fast path: inside the tick, hooked when it opened
                self._read_set.append((addr, n))
                for start, buf in snap.blocks:
                    off = addr - start
                    if off >= 0 and off + n <= len(buf):
                        self.io.hits += 1
                        return bytes(buf[off : off + n])
        elif not self.connected:
            return None
        else:
            snap = self._tick_snapshot()
        data = self._backend_read(addr, n)
        if data is not None and snap is not None:
            snap.put(addr, data)
        return data

    def write_bytes(self, addr: int, data: bytes, skip_same: bool = False) -> bool:
        """Write ``data`` at ``addr``; False if disconnected or it failed.

        ``skip_same`` (in a tick only): nothing is sent when this tick's snapshot shows the
        bytes are already there. Only for values written every tick.
        """
        if not self.connected:
            return False
        data = bytes(data)
        snap = self._tick_snapshot()
        if snap is not None and skip_same:
            if self._last_wanted.get(addr) == data:
                self._read_set.append((addr, len(data)))  # same value again: check it next tick
            self._wanted[addr] = data
            if snap.get(addr, len(data)) == data:
                self.io.skipped += 1
                return True
        t0 = time.perf_counter_ns()
        try:
            self.backend.write(addr, data)
            ok = True
        except BackendError as exc:
            self.last_error = str(exc)
            ok = False
        except Exception as exc:
            self.last_error = f"unexpected backend error: {exc}"
            ok = False
        finally:
            self._count(t0, 0, 1, len(data))
        if not ok:
            self._tick_hooked = False
        if ok and snap is not None:
            snap.patch(addr, data)
        return ok

    def snapshot_holds(self, addr: int, data: bytes) -> bool:
        """True in a tick when this tick's snapshot shows ``data`` at ``addr`` (no read is made)."""
        snap = self._tick_snapshot()
        if snap is None:
            return False
        self._read_set.append((addr, len(data)))
        return snap.get(addr, len(data)) == bytes(data)

    def read_group(self, *fields: Field) -> dict[str, tuple] | None:
        """Several fixed fields in ONE read (they must lie within MAX_SPAN bytes)."""
        layout = _GROUPS.get(fields)
        if layout is None:
            start = min(f.addr for f in fields)
            end = max(f.addr + f.size for f in fields)
            if end - start > MAX_SPAN:
                raise ValueError("fields too far apart for one read")
            parts = [(f.name, struct.Struct(f.fmt), f.addr - start) for f in fields]
            layout = _GROUPS[fields] = (start, end - start, parts)
        start, size, parts = layout
        data = self.read_bytes(start, size)
        if data is None:
            return None
        return {name: st.unpack_from(data, off) for name, st, off in parts}

    def read_field(self, field: Field) -> tuple | None:
        data = self.read_bytes(field.addr, field.size)
        return None if data is None else field.unpack(data)

    def read_value(self, field: Field):
        values = self.read_field(field)
        return None if values is None else values[0]

    def write_field(self, field: Field, *values: float | int, skip_same: bool = False) -> bool:
        return self.write_bytes(field.addr, field.pack(*values), skip_same=skip_same)

    def read_u32(self, addr: int) -> int | None:
        data = self.read_bytes(addr, 4)
        return None if data is None else struct.unpack(">I", data)[0]

    def deref(self, addr: int) -> int | None:
        """Read a pointer at ``addr``; None unless both addresses are valid RAM."""
        if not is_valid_pointer(addr):
            return None
        ptr = self.read_u32(addr)
        if ptr is None or not is_valid_pointer(ptr):
            return None
        return ptr

    def _override(self, field: Field, data: bytes, check: bool = True) -> bool:
        return self._ov[field.name].apply(self, data, check)

    def _restore(self, field: Field) -> bool:
        return self._ov[field.name].restore(self)

    # -- match -------------------------------------------------------------------

    def current_frame(self) -> int | None:
        frame = self.read_value(self.p.MATCH_FRAME)
        return None if frame is None else plausible_frame(frame)

    def stage_id(self) -> int | None:
        return self.read_value(self.p.STAGE_ID)

    def stage_pointer(self) -> int | None:
        return self.deref(self.p.G_STAGE.addr)

    def poll(self) -> PollResult:
        """Call once per sync tick: frame read, match detection, re-apply.

        New match = ``REPLAY_MODE`` rising from 0, or the entry timer going
        back to a small value, confirmed once ``g_Stage`` is valid and the
        mode is not 0. ``CAM_TYPE`` and ``MATCH_FRAME == 0`` are not used:
        both also hold in menus / during the intro.
        """
        p = self.p
        # stage id .. frame counter in one read (0x70 bytes), mode .. entry timer in one (0x32)
        match = self.read_bytes(p.STAGE_ID.addr, p.MATCH_FRAME.addr + 4 - p.STAGE_ID.addr)
        if match is None:
            return PollResult(frame=None)
        frame = plausible_frame(_U32.unpack_from(match, p.MATCH_FRAME.addr - p.STAGE_ID.addr)[0])
        (stage_id,) = _U32.unpack_from(match, 0)
        life = self.read_bytes(p.REPLAY_MODE.addr, p.MATCH_ENTRY_TIMER.addr + 2 - p.REPLAY_MODE.addr)
        mode = None if life is None else _U32.unpack_from(life, 0)[0]
        timer = None if life is None else _U16.unpack_from(life, p.MATCH_ENTRY_TIMER.addr - p.REPLAY_MODE.addr)[0]
        stage = self.stage_pointer()
        self._stage_present = stage is not None
        in_match = stage is not None and mode not in (None, 0)
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
        new_match = False
        if self._pending_match and in_match:
            self._pending_match = False
            new_match = True
            self.match_count += 1
            self._polls_since_match = 0
            self._painted.clear()  # fighters are recreated: nothing of ours left on them
            self._hidden.clear()
            self.reapply_persistent()
        elif self._polls_since_match is not None:
            self._polls_since_match += 1
            if self._polls_since_match in REAPPLY_DELAYS:
                self.reapply_persistent()
            if self._polls_since_match >= REAPPLY_DELAYS[-1]:
                self._polls_since_match = None
        return PollResult(frame=frame, new_match=new_match, stage_id=stage_id, in_match=in_match)

    def reapply_persistent(self) -> None:
        """Write again every setting the user changed (new match, re-apply button)."""
        self.read_camera_info()
        if self._music is not None:
            self._apply_music()
        if self._sfx is not None:
            self._override(self.p.SFX_VOLUME, self.p.SFX_VOLUME.pack(self._sfx))
        self._apply_shadows()
        for name, value in self._flags.items():
            self._ov[name].apply(self, bytes([value]))
        if self._lock is not None:
            self._apply_lock_copy()
        self.hold_freeze()  # a new match rebuilds the camera controller
        if self._characters is not None:
            self._apply_characters()
        self._apply_stage()
        self._apply_background()
        if self._fill is not None:
            self._apply_fill()
        if self._near is not None:
            self._apply_depth(self.p.CAM_FRONT_DEPTH, self._near)
        if self._far is not None:
            self._apply_depth(self.p.CAM_BACK_DEPTH, self._far)
        self.refresh_fighters()

    def read_camera_info(self) -> None:
        """Refresh the aspect and the stage default FOV shown by the UI."""
        aspect = self.read_value(self.p.CAM_ASPECT)
        if aspect is not None and math.isfinite(aspect) and 0.5 < aspect < 4.0:
            self.aspect = float(aspect)
        fov = self.stage_default_fov()
        if fov is not None:
            self.stage_fov = fov

    # -- camera ------------------------------------------------------------------

    def camera_kind(self) -> int | None:
        return self.read_value(self.p.CAM_CONTROLLER_KIND)

    def is_camera_paused(self) -> bool | None:
        """True in pause, False in a match, None elsewhere (menus) or unreadable."""
        kind = self.camera_kind()
        if kind == self.p.CAM_KIND_PAUSE:
            return True
        if kind == self.p.CAM_KIND_MATCH:
            return False
        return None

    def write_camera(self, origin: Sequence[float], position: Sequence[float], paused: bool | None = None) -> bool:
        """Blender-space origin (target) and position (eye) -> in-game or pause camera.

        Pause camera (experimental): the game stores the target as an OFFSET
        from the pause subject (a fighter or the stage centre) plus pitch, yaw
        and distance; the offset is computed from the subject position the
        game publishes. Nothing is written in menus.
        """
        if paused is None:
            paused = self.is_camera_paused()
            if paused is None:
                return False
        org = blender_to_brawl(origin)
        pos = blender_to_brawl(position)
        if not paused:
            return self.write_field(self.p.CAM_IG_BLOCK, *org, *pos, skip_same=True)
        subject = self.read_field(self.p.CAM_PAUSE_SUBJECT)
        if subject is None or not all(math.isfinite(c) for c in subject):
            return False
        offset = (org[0] - subject[0], org[1] - subject[1], org[2])
        pitch, yaw, distance = look_angles(org, pos)
        ok = self.write_field(self.p.CAM_PAUSE_BLOCK, *offset, pitch, yaw, distance, skip_same=True)
        # The pause controller reads gfCamera's target and angles back as its current
        # values and eases them at 30 %/frame: writing them too gives a cut on the next
        # frame (camera-motion.md 4). Target = subject + offset = org.
        ok = self.write_field(self.p.CAM_IG_ORIGIN, *org, skip_same=True) and ok
        return self.write_field(self.p.CAM_ROT_XY, pitch, yaw, skip_same=True) and ok

    def write_camera_pose(self, pose: CameraPose, cut: bool = False, suppress_shake: bool = False) -> bool:
        """Send one frame of camera, in the order of the clean-cut recipe (camera-motion.md 7).

        1. FOV (stage target, its per-frame copy, the camera), 2. roll, 3. target + eye
        in one write, 4. optionally the screen-shake offset to 0. On a ``cut`` the
        game's own copy of the lock is asserted first, so the lock holds from the
        very next frame. One tick is enough: nothing has to be repeated.
        """
        paused = self.is_camera_paused()
        if paused is None:
            return False
        if not paused and self.native_freeze and self._hold_native_freeze():
            return self._write_camera_block(pose)
        if cut:
            self.reassert_freeze()
        ok = True
        if pose.fov is not None:
            ok = self.set_fov(pose.fov) and ok
        if pose.roll is not None:
            ok = self.set_camera_roll(pose.roll) and ok
        ok = self.write_camera(pose.target, pose.eye, paused) and ok
        ok = self._write_projection(pose) and ok  # orthographic: bounds, near/far, type
        if suppress_shake:
            ok = self.suppress_screen_shake() and ok
        return ok

    def _write_camera_block(self, pose: CameraPose) -> bool:
        """Native freeze: the whole gfCamera block (0x114 bytes) in ONE write.

        Read once (this tick's snapshot), the derived fields replaced by ours
        (``camera.pack_gfcamera_block``), shake zeroed, near/far/aspect/viewport/flags kept:
        nobody else writes gfCamera while the controller is stopped. Roll and FOV not sent
        keep the current values. The roll's original is remembered for ``restore_all``.
        The projection (+0xDC..+0x104: near/far, ortho bounds, type) is in the block: an
        orthographic pose, or the way back to perspective, costs no extra call.
        """
        p = self.p
        base = p.CAM_RECORD_BASE
        current = self.read_bytes(base, p.CAM_BLOCK_SIZE)
        if current is None:
            return False

        def f32(field: Field) -> float:
            return struct.unpack_from(">f", current, field.addr - base)[0]

        roll = f32(p.CAM_ROT_Z) if pose.roll is None else float(pose.roll)
        fov = f32(p.CAM_FOV) if pose.fov is None else fov_to_game(pose.fov)
        aspect = f32(p.CAM_ASPECT)
        if valid_aspect(aspect):
            self.aspect = aspect  # what the UI shows follows the game (menus 1.65, a match 1.7323)
        else:
            aspect = self.aspect or GAME_DEFAULT_ASPECT
        if not (math.isfinite(roll) and math.isfinite(fov) and 0.001 < fov < 3.1):
            return False
        inputs = (tuple(pose.eye), tuple(pose.target), roll, fov, aspect, current)
        if self._block_cache is not None and self._block_cache[0] == inputs:
            block = bytearray(self._block_cache[1])  # same camera on the same block: same bytes
        else:
            block = bytearray(current)
            eye, target = blender_to_brawl(pose.eye), blender_to_brawl(pose.target)
            if not pack_gfcamera_block(block, eye, target, roll, fov, aspect):
                return False
            self._block_cache = (inputs, bytes(block))
        proj_at, proj_size = self._projection_range()
        proj_at -= base
        projection, restoring = self._project(bytes(block[proj_at : proj_at + proj_size]), pose)
        block[proj_at : proj_at + proj_size] = projection
        ov = self._ov[p.CAM_ROT_Z.name]
        roll_at = p.CAM_ROT_Z.addr - base
        if ov.original is None:
            ov.original = bytes(current[roll_at : roll_at + 4])
        ov.written = bytes(block[roll_at : roll_at + 4])
        ok = True
        if self._ov_stage_fov.active or self._ov[p.CAM_FOV_COPY.name].active:
            ok = self.clear_fov()  # the stage FOV is the game's again (a later hand back eases to it)
        if not self.write_bytes(base, bytes(block), skip_same=True):
            return False
        if restoring:
            self._projection = None
        return ok

    def read_camera(self) -> tuple[Vec3, Vec3] | None:
        """In-game camera -> (origin, position) in Blender space."""
        values = self.read_field(self.p.CAM_IG_BLOCK)
        if values is None:
            return None
        return brawl_to_blender(values[0:3]), brawl_to_blender(values[3:6])

    def read_camera_sample(self, frame: int | None = None) -> CameraSample | None:
        """The rendered camera (target, eye, roll, effective FOV, world matrix) in one read."""
        if frame is None:
            frame = self.current_frame()
            if frame is None:
                return None
        data = self.read_bytes(self.p.CAM_RECORD_BASE, self.p.CAM_RECORD_SIZE)
        return None if data is None else parse_camera_block(frame, data)

    def _stage_fov_address(self) -> int | None:
        stage = self.stage_pointer()
        if stage is None:
            return None
        param = self.deref(stage + self.p.STAGE_CAMERA_PARAM.offset)
        return None if param is None else param + self.p.STAGE_PARAM_FOV.offset

    def stage_default_fov(self) -> float | None:
        """The stage's own FOV (radians): the original if EBC overrides it, else the live value."""
        addr = self._stage_fov_address()
        if addr is None:
            return None
        ov = self._ov_stage_fov
        data = ov.original if ov.addr == addr and ov.original is not None else self.read_bytes(addr, 4)
        if data is None:
            return None
        (value,) = struct.unpack(">f", data)
        return value if math.isfinite(value) and 0.01 < value < 3.0 else None

    def set_fov(self, vertical_fov_rad: float) -> bool:
        """Write the vertical FOV (radians): stage FOV target, its per-frame copy, then the camera.

        Writing the camera alone is eased back towards the stage value every
        frame (10 % bias, visible shake). Stage + camera is stable but the
        first frame of a jump is still 10 % off (the copy at 0x80663EB4 is the
        easing goal of the current frame); the three together cut exactly.
        """
        value = struct.pack(">f", fov_to_game(vertical_fov_rad))
        ok = True
        addr = self._stage_fov_address()
        if addr is not None:
            ov = self._ov_stage_fov
            ov.retarget(addr)  # new stage: forget the old one
            if not ov.active:
                default = self.stage_default_fov()  # before our first write
                if default is not None:
                    self.stage_fov = default
            # The game copies the stage FOV into CAM_FOV_COPY every frame: when the copy
            # (read with this tick's snapshot) already holds what we wrote at this same
            # address, the stage FOV holds it too and writing it again changes nothing.
            if not (ov.active and ov.written == value and self.snapshot_holds(self.p.CAM_FOV_COPY.addr, value)):
                ok = ov.apply(self, value, check=False)
        ok = self._ov[self.p.CAM_FOV_COPY.name].apply(self, value, check=False, skip_same=True) and ok
        return self.write_bytes(self.p.CAM_FOV.addr, value, skip_same=True) and ok

    def clear_fov(self) -> bool:
        """Give the FOV back to the stage (the game eases the camera back to it)."""
        ok = self._ov_stage_fov.restore(self)
        return self._restore(self.p.CAM_FOV_COPY) and ok

    def set_camera_roll(self, radians: float) -> bool:
        return self._ov[self.p.CAM_ROT_Z.name].apply(
            self, self.p.CAM_ROT_Z.pack(float(radians)), check=False, skip_same=True
        )

    def clear_camera_roll(self) -> bool:
        return self._restore(self.p.CAM_ROT_Z)

    def suppress_screen_shake(self) -> bool:
        """Zero the screen-shake view offset (call every tick)."""
        return self.write_field(self.p.CAM_SHAKE, 0.0, 0.0, 0.0, skip_same=True)

    def set_front_depth(self, value: float) -> bool:
        self._near = float(value)
        return self._apply_depth(self.p.CAM_FRONT_DEPTH, self._near)

    def set_back_depth(self, value: float) -> bool:
        if value >= BACK_DEPTH_INFINITE_THRESHOLD:
            value = BACK_DEPTH_INFINITE
        self._far = float(value)
        return self._apply_depth(self.p.CAM_BACK_DEPTH, self._far)

    def _apply_depth(self, field: Field, value: float) -> bool:
        """Near / far clip of the perspective camera.

        While the game is orthographic, near/far belong to the projection EBC writes: the value
        goes into the words restored on the way back to perspective (and becomes the override's
        written value), nothing is sent now.
        """
        data = field.pack(value)
        saved = self._projection
        if saved is None:
            return self._override(field, data)
        if not self.connected:
            return False
        at = field.addr - self.p.CAM_FRONT_DEPTH.addr
        ov = self._ov[field.name]
        if ov.original is None:
            ov.original = saved[0][at : at + 4]
        ov.written = data
        near_far = bytearray(saved[0])
        near_far[at : at + 4] = data
        self._projection = (bytes(near_far), saved[1], saved[2])
        return True

    # -- projection: perspective / orthographic (docs/research/ortho-projection.md) -----------
    #
    # gfCamera+0x100 = 0 makes setGX and the g3d camera orthographic, with the bounds +0xE8..+0xF4
    # and near/far +0xDC/+0xE0. All three lie inside the native freeze block (+0x00..+0x114): the
    # projection travels in the same single write as the camera. The game's words are kept at the
    # first orthographic write and put back on the way back to perspective, by restore_all, before
    # a savestate and when the sync stops sending.

    @property
    def ortho_active(self) -> bool:
        """True while EBC holds the game in orthographic (no I/O)."""
        return self._projection is not None

    def _projection_range(self) -> tuple[int, int]:
        """(address, size) of near .. the projection type, end included: +0xDC..+0x104."""
        p = self.p
        start = p.CAM_FRONT_DEPTH.addr
        return start, p.CAM_PROJECTION.addr + 4 - start

    def _project(self, current: bytes, pose: CameraPose) -> tuple[bytes, bool]:
        """The projection words to send (+0xDC..+0x104), and whether they put the game's back.

        ``current`` = the same range as the game has it now. Ortho: the originals are captured
        first (the documented defaults when the game is already orthographic). The bounds of an
        ``OrthoLens`` use the aspect of ``current`` (+0xE4): the one the game draws with now.
        """
        p = self.p
        o_bounds = p.CAM_ORTHO_BOUNDS.addr - p.CAM_FRONT_DEPTH.addr
        o_type = p.CAM_PROJECTION.addr - p.CAM_FRONT_DEPTH.addr
        out = bytearray(current)
        if pose.ortho is None:
            saved = self._projection
            if saved is None:
                return bytes(current), False
            out[0:8], out[o_bounds : o_bounds + 16], out[o_type : o_type + 4] = saved
            return bytes(out), True
        if self._projection is None:
            if _U32.unpack_from(current, o_type)[0] == PROJECTION_PERSPECTIVE:
                self._projection = (current[0:8], current[o_bounds : o_bounds + 16], current[o_type : o_type + 4])
            else:  # already orthographic (a state saved so): the game's perspective defaults
                near_far = bytearray(struct.pack(">2f", *DEFAULT_NEAR_FAR))
                for field in (p.CAM_FRONT_DEPTH, p.CAM_BACK_DEPTH):
                    ov = self._ov[field.name]
                    if ov.written is not None:  # the user's near / far clip
                        at = field.addr - p.CAM_FRONT_DEPTH.addr
                        near_far[at : at + 4] = ov.written
                bounds = struct.pack(">4f", *DEFAULT_ORTHO_BOUNDS)
                self._projection = (bytes(near_far), bounds, _U32.pack(PROJECTION_PERSPECTIVE))
        (aspect,) = struct.unpack_from(">f", current, p.CAM_ASPECT.addr - p.CAM_FRONT_DEPTH.addr)
        if valid_aspect(aspect):
            self.aspect = aspect
        else:
            aspect = self.aspect or GAME_DEFAULT_ASPECT
        bounds = resolve_ortho(pose.ortho, aspect)
        distance = math.dist(pose.eye, pose.target)
        struct.pack_into(">2f", out, 0, ORTHO_NEAR, distance + ORTHO_FAR_MARGIN)
        struct.pack_into(">4f", out, o_bounds, *bounds)
        _U32.pack_into(out, o_type, PROJECTION_ORTHOGRAPHIC)
        return bytes(out), False

    def _write_projection(self, pose: CameraPose) -> bool:
        """Code Menu lock / pause camera: the projection words in their own write (0x28 bytes).

        Nothing at all (no read) while perspective with nothing of ours in place.
        """
        if pose.ortho is None and self._projection is None:
            return True
        addr, size = self._projection_range()
        current = self.read_bytes(addr, size)
        if current is None:
            return False
        data, restoring = self._project(current, pose)
        if not self.write_bytes(addr, data, skip_same=not restoring):
            return False
        if restoring:
            self._projection = None
        return True

    def restore_projection(self) -> bool:
        """Back to the game's perspective (+0x100 = 1, bounds and near/far as they were).

        A no-op without I/O when EBC never made the game orthographic.
        """
        if self._projection is None:
            return True
        if not self.connected:
            return False
        addr, size = self._projection_range()
        current = self.read_bytes(addr, size, fresh=True)
        if current is None:
            return False
        data, _ = self._project(current, CameraPose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        if not self.write_bytes(addr, data):
            return False
        self._projection = None
        return True

    # -- fighters ----------------------------------------------------------------

    def fighters(self) -> dict[int, FighterRef]:
        """Every fighter in the match, by 0-based port. Resolve once per tick.

        Same chain as ftManager::getFighter: entry -> m_instances[active].
        Entries are in creation order, so the port comes from the entry.
        Zelda/Sheik and Pokemon Trainer switch instance: resolving every
        tick follows them. Every pointer is range-checked.
        """
        p = self.p
        entries = self.deref(p.FT_ENTRY_MANAGER.addr)
        if entries is None:
            return {}
        # Only the used part of the table: count, active, port and the instances of each entry
        # (+0x08..+0x48 of 0x244), i.e. from entry 0 +0x08 to the last entry +0x48.
        first = p.ENTRY_INSTANCE_COUNT.offset
        last = p.ENTRY_INSTANCES.offset + p.ENTRY_MAX_INSTANCES * p.ENTRY_INSTANCE_STRIDE
        used = self.read_bytes(entries + first, (p.FT_ENTRY_COUNT - 1) * p.FT_ENTRY_STRIDE + last - first)
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
            modules = self.deref(fighter + p.FIGHTER_MODULES.offset)
            if modules is None:
                continue
            ref = FighterRef(port, entries + base, kind, fighter, modules, [modules])
            if kind in (p.KIND_POPO, p.KIND_NANA):
                # Ice Climbers (deduced): the partner instance is active too.
                for k, (other_kind, other) in enumerate(instances):
                    if k != active and other_kind in (p.KIND_POPO, p.KIND_NANA) and is_valid_pointer(other):
                        other_modules = self.deref(other + p.FIGHTER_MODULES.offset)
                        if other_modules is not None:
                            ref.all_modules.append(other_modules)
            refs[port] = ref
        return refs

    def fighter(self, port: int) -> FighterRef | None:
        return self.fighters().get(port)

    def fighter_address(self, port: int) -> int | None:
        ref = self.fighter(port)
        return None if ref is None else ref.fighter

    def character_id(self, port: int) -> int | None:
        """ftKind of the active form (0 = Mario is valid), None for an empty port."""
        ref = self.fighter(port)
        return None if ref is None else ref.kind

    def is_player_present(self, port: int) -> bool:
        return self.fighter(port) is not None

    def player_position(self, port: int, refs: dict[int, FighterRef] | None = None) -> Vec3 | None:
        """Ground position of the fighter in Blender space, or None."""
        ref = (self.fighters() if refs is None else refs).get(port)
        if ref is None:
            return None
        p = self.p
        posture = self.deref(ref.modules + p.MODULES_POSTURE.offset)
        if posture is None:
            return None
        data = self.read_bytes(posture + p.POSTURE_POSITION.offset, p.POSTURE_POSITION.size)
        if data is None:
            return None
        xyz = struct.unpack(p.POSTURE_POSITION.fmt, data)
        if not all(math.isfinite(c) and abs(c) < 1e6 for c in xyz):
            return None
        return brawl_to_blender(xyz)

    def _guarded_module(self, modules: int, slot_offset: int, guard_offset: int) -> int | None:
        module = self.deref(modules + slot_offset)
        if module is None or self.read_u32(module + guard_offset) != modules:
            return None  # not the object we think it is: write nothing
        return module

    def color_blend_module(self, port: int) -> int | None:
        ref = self.fighter(port)
        if ref is None:
            return None
        return self._guarded_module(ref.modules, self.p.MODULES_COLOR_BLEND.offset, self.p.CBM_ACCESSER.offset)

    def visibility_module(self, port: int) -> int | None:
        ref = self.fighter(port)
        if ref is None:
            return None
        return self._guarded_module(ref.modules, self.p.MODULES_VISIBILITY.offset, self.p.VIS_ACCESSER.offset)

    # -- per-fighter mode: normal, colour, hidden ---------------------------------------
    #
    # Colour: soColorBlendModule "sub colour", RGBA at cbm+0x14A, enable at
    # cbm+0x14F (what setSubColor writes, nothing else). Hidden:
    # soVisibilityModule+0x0E = 0. A KO/respawn resets both, so they are
    # written on every tick while COLOUR/HIDDEN, and cleared once on NORMAL.

    def set_fighter_mode(self, port: int, mode: str, rgba: Sequence[float] = (0.0, 0.0, 0.0, 0.0)) -> bool:
        """NORMAL (untouched), COLOUR (``rgba`` floats 0..1, alpha = tint strength,
        1 = flat colour) or HIDDEN (not drawn)."""
        if mode not in FIGHTER_MODES:
            raise ValueError(f"unknown fighter mode {mode!r}")
        state = self._fighter_modes.setdefault(port, _FighterMode())
        state.mode = mode
        state.rgba = color_to_bytes(rgba)
        return self.refresh_fighters() >= 0 and self.connected

    def needs_fighter_refresh(self) -> bool:
        return (
            any(m.mode != FIGHTER_NORMAL for m in self._fighter_modes.values())
            or any(self._painted.values())
            or any(self._hidden.values())
        )

    def refresh_fighters(self, refs: dict[int, FighterRef] | None = None) -> int:
        """Apply colours/hides to the current fighters. Call every tick. Returns the writes made."""
        if not self.connected or not self.needs_fighter_refresh():
            return 0
        if refs is None:
            refs = self.fighters()
        p = self.p
        writes = 0
        for port in range(p.FT_ENTRY_COUNT):
            state = self._fighter_modes.get(port, _FighterMode())
            ref = refs.get(port)
            color = state.rgba if state.mode == FIGHTER_COLOUR else None
            hide = state.mode == FIGHTER_HIDDEN
            painted = self._painted.setdefault(port, {})
            hidden = self._hidden.setdefault(port, {})
            for modules in ref.all_modules if ref is not None else ():
                cbm = self._guarded_module(modules, p.MODULES_COLOR_BLEND.offset, p.CBM_ACCESSER.offset)
                if cbm is not None and color is not None:
                    # every tick (a KO/respawn resets the channel), sent only when it differs
                    self.write_bytes(cbm + p.CBM_SUB_COLOR.offset, color, skip_same=True)
                    self.write_bytes(cbm + p.CBM_SUB_ENABLE.offset, b"\x01", skip_same=True)
                    painted[cbm] = modules
                    writes += 1
                vis = self._guarded_module(modules, p.MODULES_VISIBILITY.offset, p.VIS_ACCESSER.offset)
                if vis is not None and hide:
                    self.write_bytes(vis + p.VIS_VISIBLE.offset, b"\x00", skip_same=True)
                    hidden[vis] = modules
                    writes += 1
            if color is None:
                writes += self._clear_painted(painted)
            if not hide:
                writes += self._clear_hidden(hidden)
        return writes

    def _still_ours(self, module: int, guard_offset: int, modules: int) -> bool:
        return self.read_u32(module + guard_offset) == modules

    def _clear_painted(self, painted: dict[int, int]) -> int:
        p = self.p
        n = 0
        for cbm, modules in list(painted.items()):
            if self._still_ours(cbm, p.CBM_ACCESSER.offset, modules):
                self.write_bytes(cbm + p.CBM_SUB_COLOR.offset, _CLEAR)
                self.write_bytes(cbm + p.CBM_SUB_ENABLE.offset, b"\x00")
                n += 1
            del painted[cbm]
        return n

    def _clear_hidden(self, hidden: dict[int, int]) -> int:
        p = self.p
        n = 0
        for vis, modules in list(hidden.items()):
            if self._still_ours(vis, p.VIS_ACCESSER.offset, modules):
                self.write_bytes(vis + p.VIS_VISIBLE.offset, b"\x01")  # once: never force 1 in a loop
                n += 1
            del hidden[vis]
        return n

    def _clear_fighters(self) -> bool:
        if not self.connected:
            return False
        for painted in self._painted.values():
            self._clear_painted(painted)
        for hidden in self._hidden.values():
            self._clear_hidden(hidden)
        return True

    # -- Code Menu toggles (restored on disconnect) -----------------------------------

    def _set_flag(self, field: Field, value: int | bool) -> bool:
        self._flags[field.name] = int(value)
        return self._ov[field.name].apply(self, bytes([int(value)]))

    def set_hud(self, enabled: bool) -> bool:
        return self._set_flag(self.p.DISPLAY_HUD, enabled)

    def set_debug_menu(self, enabled: bool) -> bool:
        return self._set_flag(self.p.DEBUG_MENU, enabled)

    def set_draw_di(self, enabled: bool) -> bool:
        return self._set_flag(self.p.DRAW_DI, enabled)

    # -- camera freeze: the ONE entry point -------------------------------------------
    #
    # Everything outside this block (UI, sync, cuts, hand back) calls freeze_camera /
    # reassert_freeze / hold_freeze / camera_frozen only. Two mechanisms:
    #
    # * FREEZE_NATIVE (default, docs/research/camera-freeze.md recipe R2): clear bit 0x10 of
    #   cmAIController+0x94. The match camera, its quake and the subject camera stop, and
    #   nobody calls gfCamera::update: write_camera_pose then writes the whole derived
    #   gfCamera block in one write. Exact from the next frame, free target Z, no shake,
    #   back to our camera after a pause, no Code Menu. Releasing it = the game glides from
    #   our camera (Hand back). The bit is lost with a new controller: held every tick.
    # * FREEZE_CODE_MENU (legacy): the P+ Code Menu lock and its game copy.

    def set_freeze_method(self, method: str) -> bool:
        """Switch mechanism; a freeze in place is moved to the new one."""
        if method not in FREEZE_METHODS:
            raise ValueError(f"unknown freeze method {method!r}")
        if method == self.freeze_method:
            return True
        frozen = self._frozen
        ok = self.freeze_camera(False) if frozen else True
        self.freeze_method = method
        return (self.freeze_camera(True) if frozen else True) and ok

    def freeze_camera(self, frozen: bool) -> bool:
        """Freeze the game camera (what EBC writes stays) or give it back to the game.

        Restored by ``restore_all``.
        """
        self._frozen = bool(frozen)
        if self.freeze_method == FREEZE_NATIVE:
            return self._hold_native_freeze() if frozen else self._release_native_freeze()
        return self.set_camera_lock(frozen)

    def reassert_freeze(self) -> bool:
        """Before a cut: make sure the freeze holds from the very next frame."""
        ok = self.assert_camera_lock()  # the Code Menu lock, if one is set (legacy, tools)
        if self.freeze_method == FREEZE_NATIVE and self._frozen:
            ok = self._hold_native_freeze() and ok
        return ok

    def hold_freeze(self) -> bool:
        """Per tick: keep the native freeze (a new match or a savestate brings the bit back).

        Free when it holds: the flags byte is in the tick snapshot, nothing is written.
        """
        if self.freeze_method != FREEZE_NATIVE or not self._frozen:
            return True
        return self._hold_native_freeze()

    @property
    def camera_frozen(self) -> bool:
        return bool(self._frozen)

    @property
    def native_freeze(self) -> bool:
        """Frozen with the native mechanism: the target may leave the stage plane."""
        return self._frozen and self.freeze_method == FREEZE_NATIVE

    def _ai_flags_address(self) -> int | None:
        p = self.p
        cc = self.deref(p.CAMERA_CONTROLLER.addr)
        if cc is None:
            return None
        ai = self.deref(cc + p.CC_AI_CONTROLLER.offset)
        if ai is None or self.read_u32(ai + p.AI_CAMERA.offset) != p.CAM_RECORD_BASE:
            return None  # not the match controller we know: write nothing
        return ai + p.AI_FLAGS.offset

    def _hold_native_freeze(self) -> bool:
        """Clear the update bit (original remembered). True when the freeze holds."""
        if self._held_in_tick and self._tick_snapshot() is not None:
            return True  # already checked in this tick
        addr = self._ai_flags_address()
        if addr is None:
            return False
        bit = self._bit_ai
        bit.retarget(addr)
        current = self.read_bytes(addr, 1)  # tick snapshot: already clear = nothing to do
        held = (current is not None and bit.active and not current[0] & bit.bit) or bit.apply(self, False)
        self._held_in_tick = held and self._tick_snapshot() is not None
        return held

    def _release_native_freeze(self) -> bool:
        self._held_in_tick = False
        bit = self._bit_ai
        if not bit.active:
            return True
        if self._ai_flags_address() != bit.addr:
            bit.forget()  # the controller is gone: nothing of ours left
            return True
        return bit.restore(self)

    def set_camera_lock(self, enabled: bool) -> bool:
        """Implementation of ``freeze_camera``: Code Menu lock AND the game's own copy (u16 0x80583FFA).

        P+ copies the menu value once per frame, too late for the current one:
        writing the copy too makes the lock (or its release) effective on the
        next frame. Both are restored by ``restore_all``.
        """
        self._lock = bool(enabled)
        ok = self._set_flag(self.p.CAM_LOCK, enabled)
        return self._apply_lock_copy() and ok

    def _apply_lock_copy(self) -> bool:
        return self._override(self.p.CAM_LOCK_GAME, self.p.CAM_LOCK_GAME.pack(1 if self._lock else 0))

    def assert_camera_lock(self) -> bool:
        """Write the lock copy again if the lock is wanted (before a cut)."""
        if not self._lock:
            return True
        return self._override(self.p.CAM_LOCK_GAME, self.p.CAM_LOCK_GAME.pack(1), check=False)

    # -- characters / stage visibility (persist across matches: restored) ----------

    def set_characters_display(self, mode: str) -> bool:
        if mode not in _CHARACTERS_MODES:
            return False
        self._characters = mode
        return self._apply_characters()

    def _apply_characters(self) -> bool:
        hurtbox, visible = _CHARACTERS_MODES[self._characters or CHARACTERS_MODEL]
        p = self.p
        # Measured: hurtbox value 2 clears this same bit, and the 2 -> 0
        # transition sets it again on the next frame. Capture the original
        # before touching the hurtbox value; HIDDEN is re-asserted per tick.
        self._bit_characters.capture(self)
        ov = self._ov[p.DISPLAY_HURTBOX.name]
        ok = ov.apply(self, bytes([hurtbox])) if hurtbox else ov.restore(self)
        bit = self._bit_characters
        if not visible:
            return bit.apply(self, False) and ok
        if hurtbox == 2:
            return ok  # the game hides the models itself: keep the captured original
        return bit.restore(self) and ok

    def assert_display(self) -> bool:
        """Per-tick: keep the characters hidden (the game may set the bit back, see above)."""
        if self._characters != CHARACTERS_HIDDEN:
            return True
        return self._bit_characters.apply(self, False)

    def set_stage_display(self, mode: str) -> bool:
        if mode not in _STAGE_MODES:
            return False
        self._stage = mode
        return self._apply_stage()

    def _apply_stage(self) -> bool:
        collision, visible = _STAGE_MODES[self._stage or STAGE_VISIBLE]
        ov = self._ov[self.p.DISPLAY_STAGE_COLLISION.name]
        ok = ov.apply(self, bytes([collision])) if collision else ov.restore(self)
        bit = self._bit_stage
        return (bit.apply(self, False) if not visible else bit.restore(self)) and ok

    # -- audio (persistent, restored on disconnect) -------------------------------------

    def set_music_volume(self, percent: float) -> bool:
        self._music = max(0.0, min(100.0, float(percent))) / 100.0
        return self._apply_music()

    def music_volume_address(self) -> int | None:
        """[[SOUND_SETTINGS]] + 0x80, guarded by the object's back pointer; the fixed
        3.1.5 address when the chain cannot be followed (None on 3.2)."""
        p = self.p
        obj = self.deref(p.SOUND_SETTINGS.addr)
        if obj is not None and self.read_u32(obj + p.BGM_BACKREF.offset) == p.SOUND_SETTINGS.addr:
            return obj + p.BGM_VOLUME.offset
        return None if p.MUSIC_VOLUME is None else p.MUSIC_VOLUME.addr

    def _apply_music(self) -> bool:
        addr = self.music_volume_address()
        if addr is None or self._music is None:
            self.last_error = "music volume: sound object not found"
            return False
        self._ov_music.retarget(addr)
        return self._ov_music.apply(self, struct.pack(">f", self._music))

    def set_sfx_volume(self, percent: float) -> bool:
        self._sfx = max(0.0, min(100.0, float(percent))) / 100.0
        return self._override(self.p.SFX_VOLUME, self.p.SFX_VOLUME.pack(self._sfx))

    # -- shadows (light direction in degrees; reset by the game on stage load) ----------

    def set_shadow_direction(self, x: float | None = None, y: float | None = None) -> bool:
        if x is not None:
            self._shadow_dir[0] = float(x)
        if y is not None:
            self._shadow_dir[1] = float(y)
        return self._apply_shadows()

    def _apply_shadows(self) -> bool:
        ok = True
        for field_, value in zip((self.p.SHADOW_X, self.p.SHADOW_Y), self._shadow_dir, strict=True):
            if value is not None:
                ok = self._override(field_, field_.pack(value)) and ok
        return ok

    # -- full-screen background (efScreen fill layer) and void colour (EFB clear colour) --
    #
    # docs/research/green-screen-alpha.md. The game's screen-fill manager (efScreen, the one
    # that dims the screen during a Final Smash) draws layer 0 after the stage pass, under the
    # fighters, items, effects and HUD. EBC writes its own request (n° 9, the last one: the game
    # allocates from 0) as a held flat fill and chains it in layer 0: its alpha is exact
    # (src * A + dst * (1 - A) over the stage), A = 255 is a flat green screen with the stage
    # still drawn. Data only, no code patch. The game empties the layer when a match ends and a
    # savestate brings its own: ``hold_green_screen`` repairs it on every sync tick, for free
    # when it holds (the request and the layer are in the tick snapshot). Never written outside
    # a match (g_Stage null). The void colour is the EFB clear colour: what shows where nothing
    # is drawn (around the stage, everywhere once the stage is hidden); its alpha does nothing.

    def set_green_screen(self, enabled: bool, rgba: Sequence[float]) -> bool:
        """Full-screen background: a flat ``rgba`` fill over the stage, under the fighters.

        The alpha is effective (0 = invisible, 1 = opaque). Outside a match the colour is kept
        and the layer is laid at the next tick in a match. Off: request 9 is unchained and
        given back as it was.
        """
        if not enabled:
            self._fill = None
            return self._release_fill()
        self._fill = color_to_bytes(rgba)
        return self._apply_fill()

    def hold_green_screen(self) -> bool:
        """Per tick: lay the fill again if the game dropped it (end of match, savestate).

        Uses the g_Stage check of this tick's ``poll``; served by the tick snapshot, so it costs
        no call while the layer holds.
        """
        if self._fill is None:
            return True
        return self._apply_fill(self._stage_present, held=True)

    @property
    def green_screen(self) -> bool:
        return self._fill is not None

    def _fill_request(self, base: int) -> int:
        p = self.p
        return base + p.EFSCREEN_REQUESTS.offset + p.EFSCREEN_REQUEST_STRIDE * p.EFSCREEN_REQUEST_INDEX

    def _efscreen(self) -> int | None:
        base = self.deref(self.p.EFSCREEN_PTR.addr)
        if base is None or not MEM1_START <= base < MEM1_END:
            return None
        return base

    def _is_our_fill(self, data: bytes, at: int) -> bool:
        """Request 9 is ours and in place (active held flat fill, layer 0, priority FF) and chained."""
        p = self.p
        request = data[at : at + 8]
        return (
            p.EFSCREEN_REQUEST_INDEX in data[1 : 1 + p.EFSCREEN_SLOTS]
            and request[p.REQ_ACTIVE.offset] == 1
            and request[p.REQ_TYPE.offset] == 2
            and request[3:5] == _FILL_HEAD[3:5]
        )

    def _apply_fill(self, in_match: bool | None = None, held: bool = False) -> bool:
        color = self._fill
        if color is None or not self.connected:
            return False
        if in_match is None:
            in_match = self.stage_pointer() is not None
        if not in_match:
            return True  # menus: nothing to draw on, laid again in the next match
        p = self.p
        size = self._fill_request(0) + p.EFSCREEN_REQUEST_STRIDE  # layer 0 .. request 9: one read
        data = None
        base = self._fill_base if held else None
        if base is not None:
            # Per tick, while it holds: our own request found where we laid it this session
            # (its signature checked on this tick's bytes) proves the efScreen there is still
            # the live one, without reading g_efScreen again. Anything else: full path below.
            data = self.read_bytes(base, size)
            if data is None or not self._is_our_fill(data, size - p.EFSCREEN_REQUEST_STRIDE):
                data = None
        if data is None:
            base = self._efscreen()
            if base is None:
                return False
            data = self.read_bytes(base, size)
            if data is None:
                return False
        req = self._fill_request(base)
        at = req - base
        if self._fill_base != base:
            self._fill_base = base
            self._fill_original = data[at : at + p.EFSCREEN_REQUEST_STRIDE]
        colors = color * 3
        linked = p.EFSCREEN_REQUEST_INDEX in data[1 : 1 + p.EFSCREEN_SLOTS]
        if self._is_our_fill(data, at):
            # in place: only the colour, sent when it differs from what the game holds
            return self.write_bytes(req + p.REQ_COLORS.offset, colors, skip_same=True)
        if not linked and _FREE_SLOT not in data[1 : 1 + p.EFSCREEN_SLOTS]:
            self.last_error = "efScreen layer 0 is full"
            return False  # nothing written: the request is not laid without a slot for it
        ok = self.write_bytes(req, _FILL_HEAD + colors)
        if not linked:
            ok = ok and self._link_fill(base)
        return ok

    def _link_fill(self, base: int) -> bool:
        """Chain request 9 in the first free slot of layer 0 (count + 1)."""
        p = self.p
        layer = self.read_bytes(base, p.EFSCREEN_LAYER0.size, fresh=True)  # the game owns it
        if layer is None:
            return False
        slots = list(layer[1:])
        if p.EFSCREEN_REQUEST_INDEX in slots:
            return True
        if _FREE_SLOT not in slots:
            self.last_error = "efScreen layer 0 is full"
            return False
        slots[slots.index(_FREE_SLOT)] = p.EFSCREEN_REQUEST_INDEX
        return self.write_bytes(base, bytes([(layer[0] + 1) & 0xFF, *slots]))

    def _release_fill(self) -> bool:
        """Unchain request 9 (shifting the next slots, count - 1) and give it back.

        The layer is never rewritten from a copy: a Final Smash may have chained itself since.
        The request is only given back while it is ours (chained by us, or free again).
        """
        base = self._fill_base
        if base is None:
            return True
        if not self.connected:
            return False
        p = self.p
        if self._efscreen() != base:
            self._fill_base = self._fill_original = None  # another efScreen: nothing of ours
            return True
        layer = self.read_bytes(base, p.EFSCREEN_LAYER0.size, fresh=True)
        if layer is None:
            return False
        index = p.EFSCREEN_REQUEST_INDEX
        slots = list(layer[1:])
        ok = True
        linked = index in slots
        if linked:
            k = slots.index(index)
            slots = [*slots[:k], *slots[k + 1 :], _FREE_SLOT]
            ok = self.write_bytes(base, bytes([(layer[0] - 1) & 0xFF, *slots]))
        req = self._fill_request(base)
        request = self.read_bytes(req, p.EFSCREEN_REQUEST_STRIDE, fresh=True)
        if ok and request is not None and (linked or request[p.REQ_ACTIVE.offset] == 0):
            ok = self.write_bytes(req + p.REQ_ACTIVE.offset, b"\x00")
            original = self._fill_original
            if ok and original is not None:
                # the game's bytes, free (active 0: 0xCC on a fresh memory would keep it reserved)
                ok = self.write_bytes(req, original[:1] + b"\x00" + original[2:])
        if ok:
            self._fill_base = self._fill_original = None
        return ok

    def set_stage_void_color(self, enabled: bool, rgb: Sequence[float]) -> bool:
        """Void colour: the EFB clear colour, seen around the stage and behind a hidden stage.

        Only RGB has an effect (measured); the alpha written is 255.
        """
        self._background = color_to_bytes(tuple(rgb)[:3]) if enabled else None
        return self._apply_background()

    def _apply_background(self) -> bool:
        if self._background is None:
            return self._restore(self.p.BACKGROUND_COLOR)
        return self._override(self.p.BACKGROUND_COLOR, self._background)


# -- Camera optics (pure math, mirrors Blender's sensor-fit rules) -------------


def _vertical_sensor(sensor_width: float, sensor_height: float, sensor_fit: str, res_x: float, res_y: float) -> float:
    if res_x <= 0 or res_y <= 0:
        return sensor_height
    if sensor_fit == "AUTO":
        # AUTO applies sensor_width to the larger image dimension.
        return sensor_width * res_y / res_x if res_x >= res_y else sensor_width
    if sensor_fit == "HORIZONTAL":
        return sensor_width * res_y / res_x
    return sensor_height  # VERTICAL


def vertical_fov(
    lens: float, sensor_width: float, sensor_height: float, sensor_fit: str, res_x: float, res_y: float
) -> float:
    """Full vertical field of view (radians) of a Blender perspective camera.

    This is what the game takes. It depends on Blender's render aspect
    (res_x / res_y): set the render resolution to the game aspect (1.7323,
    e.g. 834 x 480) for the horizontal framing to match as well.
    """
    sensor = _vertical_sensor(sensor_width, sensor_height, sensor_fit, res_x, res_y)
    return 2.0 * math.atan(sensor / (2.0 * lens))


def lens_for_vertical_fov(
    fov_rad: float, sensor_width: float, sensor_height: float, sensor_fit: str, res_x: float, res_y: float
) -> float:
    """Focal length (mm) giving ``fov_rad`` vertically. Inverse of ``vertical_fov``."""
    sensor = _vertical_sensor(sensor_width, sensor_height, sensor_fit, res_x, res_y)
    return sensor / (2.0 * math.tan(fov_rad / 2.0))


def aspect_settings(aspect: float, resolution_y: int) -> tuple[int, float, float]:
    """Render (resolution_x, pixel_aspect_x, pixel_aspect_y) giving exactly ``aspect`` at this height.

    The width is rounded to whole pixels and the pixel aspect absorbs the
    rounding, so ``res_x * pax / (res_y * pay) == aspect``. Blender's pixel
    aspects are >= 1: the one above 1 is the one that compensates.
    """
    ry = max(1, int(resolution_y))
    rx = max(1, round(aspect * ry))
    ratio = aspect * ry / rx  # pax / pay
    if ratio >= 1.0:
        return rx, ratio, 1.0
    return rx, 1.0, 1.0 / ratio
