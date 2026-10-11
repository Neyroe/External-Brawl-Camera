"""Stages: identity of the stage being played, the shipped stage table, stage camera views, preset
selection and "Reload on stage". No ``bpy``.

Research: docs/research/stage-identity.md (identity, table, stage camera) and
docs/research/stage-reload.md (reload by data writes). Every memory access goes through the
``Game`` (``read_bytes`` / ``write_bytes`` / ``deref``): nothing is written while disconnected.

Identity key = ``<slot hex>:<.pac stem>`` (``01:BATTLEFIELD_L_BF_MELEE``): the P+ slot alone is
shared by the ASL variants, and the slot + button pair changes between 3.1.5 and 3.2; the .pac
loaded is what is drawn. Smashville's time of day is grouped under the STEX .pac name
(``21:SMASHVILLE``). Valid only in ``scMelee`` once ``g_Stage`` is set, and never for the menus /
results slots (0x26 / 0x28); the loader path still holds the previous ``.pac`` for one frame at the
start of the match, so a path that does not belong to the STEX of the slot is "not ready yet".
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .addresses import PPlus315

if TYPE_CHECKING:
    from .game import Game

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "stages.json"
# Slots that are not stages to play: menus, results, Home-Run, Stage Builder, All-Star rest area,
# Training room, Break the Targets, credits, Subspace (stage-reload.md 4.3, stage-identity.md 2.1).
NOT_PLAYABLE = frozenset({0x26, 0x28, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x4E})
RESULTS_PAC = "RESULT"  # STGResult.PAC: the results screen's stage, left in the loader for one frame


def _cstr(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("latin1")


# -- table -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    slot: int
    button: int
    label: str  # '' base, 'L' / 'R' / 'Z', else the .param name (random / forced alternates)
    pac: str  # STEX .pac name
    stems: tuple[str, ...]  # .pac file stems when they differ from ``pac`` (substages, time of day)
    stage_name: str

    @property
    def name(self) -> str:
        return f"{self.stage_name} ({self.label})" if self.label else self.stage_name

    @property
    def key_stem(self) -> str:
        return self.pac.upper()

    def matches(self, stem: str) -> bool:
        stem = stem.upper()
        return stem == self.pac.upper() or any(stem == s.upper() for s in self.stems)


@lru_cache(maxsize=1)
def load_table() -> dict[str, dict[int, tuple[str, tuple[Variant, ...]]]]:
    """``{profile name: {slot: (stage name, variants)}}`` from ebc/data/stages.json."""
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    out: dict[str, dict[int, tuple[str, tuple[Variant, ...]]]] = {}
    for version, slots in raw.items():
        table = {}
        for key, entry in slots.items():
            slot = int(key, 16)
            name = entry["n"]
            variants = tuple(Variant(slot, v["b"], v["l"], v["p"], tuple(v.get("f", ())), name) for v in entry["v"])
            table[slot] = (name, variants)
        out[version] = table
    return out


def table_for(profile_name: str) -> dict[int, tuple[str, tuple[Variant, ...]]]:
    tables = load_table()
    return tables.get(profile_name) or tables[PPlus315.name]


def stage_name(profile_name: str, slot: int) -> str | None:
    entry = table_for(profile_name).get(slot)
    return entry[0] if entry else None


def find_variant(profile_name: str, slot: int, stem: str, asl: int | None = None) -> Variant | None:
    """The table variant of ``slot`` whose .pac is ``stem`` (the one with button ``asl`` first)."""
    entry = table_for(profile_name).get(slot)
    if entry is None:
        return None
    hits = [v for v in entry[1] if v.matches(stem)]
    for v in hits:
        if asl is not None and v.button == asl:
            return v
    return hits[0] if hits else None


def reload_choices(profile_name: str) -> list[Variant]:
    """Every playable stage of the version: base first, then L / R / Z, then the alternates.

    Two buttons that load the same .pac are listed once (the first button).
    """
    out = []
    for slot, (_name, variants) in sorted(table_for(profile_name).items(), key=lambda kv: kv[1][0].lower()):
        if slot in NOT_PLAYABLE:
            continue
        seen = set()
        for v in variants:
            if v.pac.upper() in seen:
                continue
            seen.add(v.pac.upper())
            out.append(v)
    return out


# -- identity ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageIdentity:
    slot: int
    stem: str  # key part: the .pac file stem, or the STEX .pac name (Smashville)
    file: str  # .pac file actually loaded, without STG / .PAC
    asl: int  # ASL mask of the variant P+ loaded
    stage_kind: int | None
    name: str  # display name: table name (+ variant label), else the .pac stem

    @property
    def key(self) -> str:
        return identity_key(self.slot, self.stem)

    @property
    def label(self) -> str:
        """'Battlefield (L) — slot 01 · BATTLEFIELD_L_BF_MELEE'"""
        return f"{self.name} — slot {self.slot:02X} · {self.stem}"


def identity_key(slot: int, stem: str) -> str:
    return f"{slot:02X}:{stem.upper()}"


def split_key(key: str) -> tuple[int, str] | None:
    try:
        slot, stem = key.split(":", 1)
        return int(slot, 16), stem
    except (ValueError, AttributeError):
        return None


def _pac_stem(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name.upper().startswith("STG") and name.upper().endswith(".PAC") and len(name) > 7:
        return name[3:-4]
    return None


def read_cstr(game: Game, ptr_addr: int, n: int = 24) -> str | None:
    ptr = game.deref(ptr_addr)
    if ptr is None:
        return None
    data = game.read_bytes(ptr, n)
    return None if data is None else _cstr(data)


def scene_pointers(game: Game) -> tuple[int | None, int | None]:
    """(current gfScene*, current gfSequence*)."""
    p = game.p
    mgr = game.deref(p.SCENE_MANAGER.addr)
    if mgr is None:
        return None, None
    return game.deref(mgr + p.MGR_SCENE.offset), game.deref(mgr + p.MGR_SEQUENCE.offset)


def mode_melee(game: Game) -> int | None:
    gg = game.deref(game.p.GAME_GLOBAL.addr)
    return None if gg is None else game.deref(gg + game.p.GG_MODE_MELEE.offset)


@dataclass(frozen=True)
class Stex:
    """The STEX file P+ loaded for the current slot."""

    pac: str  # .pac name ('' when the file has none)
    flags: int
    game_suffix: bool  # suffix kept from the game: identified by ``pac`` (Smashville, every time of day)


def read_stex(game: Game) -> Stex | None:
    """The STEX file P+ loaded for the current slot: .pac name and flags."""
    p = game.p
    head = game.read_bytes(p.STEX.addr, p.STEX.size)
    if head is None or head[:4] != b"STEX":
        return None
    strings, size = struct.unpack_from(">II", head, p.STEX_STRINGS)
    if not 0x2C <= strings < 0x200 or not strings < size <= 0x200:
        return None
    blob = game.read_bytes(p.STEX.addr, size)
    if blob is None:
        return None
    (pac_off,) = struct.unpack_from(">I", head, p.STEX_PAC)
    at = strings + pac_off
    pac = _cstr(blob[at:]) if at < len(blob) else ""
    (flags,) = struct.unpack_from(">H", head, p.STEX_FLAGS)
    return Stex(pac, flags, bool(flags & p.STEX_FLAG_GAME_SUFFIX))


def identify(game: Game, profile_name: str | None = None, accept_stex: bool = False) -> StageIdentity | None:
    """The stage being played (identify() of stage_probe), None when not (yet) valid.

    Pure reads (about 12 calls): only on a new match. ``accept_stex``: take the STEX .pac name when
    the loader path cannot be matched to it (fallback after several tries).
    """
    p = game.p
    scene, _seq = scene_pointers(game)
    if scene is None or read_cstr(game, scene) != p.SCENE_MATCH:
        return None
    stage = game.stage_pointer()
    if stage is None:
        return None
    mm = mode_melee(game)
    if mm is None:
        return None
    slot = game.read_value_at(mm + p.MM_STAGE_SLOT.offset, p.MM_STAGE_SLOT.fmt)
    if slot is None or slot in (p.SLOT_MENUS, p.SLOT_RESULTS):
        return None
    kind = game.read_value_at(stage + p.STAGE_KIND.offset, p.STAGE_KIND.fmt)
    stex_slot = game.read_value(p.STEX_SLOT)
    asl = game.read_value(p.STEX_ASL_MASK) or 0
    stex = read_stex(game) if stex_slot == slot else None
    profile = profile_name or p.name
    file = _checked_file(_loader_file(game), stex, profile, slot, accept_stex)
    if file is None:
        if not (accept_stex and stex is not None and stex.pac):
            return None
        file = stex.pac
    stem = stex.pac if stex is not None and stex.game_suffix and stex.pac else file
    variant = find_variant(profile, slot, file, asl)
    if variant is not None:
        name = variant.name
    else:
        base = stage_name(profile, slot)
        name = f"{base} ({file})" if base else file
    return StageIdentity(slot, stem.upper(), file, asl, kind, name)


def _loader_file(game: Game) -> str | None:
    """The .pac stem the stage loader is loading (its path), None when it has none yet."""
    p = game.p
    path = ""
    lm = game.deref(p.LOADER_MANAGER.addr)
    loader = None if lm is None else game.deref(lm + p.LM_STAGE_LOADER.offset)
    if loader is not None:
        data = game.read_bytes(loader + p.LOADER_PATH.offset, p.LOADER_PATH.size)
        path = "" if data is None else _cstr(data)
    return _pac_stem(path)


def _checked_file(file: str | None, stex: Stex | None, profile: str, slot: int, accept_stex: bool) -> str | None:
    """The loader's file when it is this slot's stage, else None (not ready: the previous screen's
    path, another slot's file, or nothing to check it against yet)."""
    if file is None:
        return None
    if file.upper() == RESULTS_PAC:
        return None  # the previous screen's path (first frame of scMelee): not ready
    if stex is not None and not file.upper().startswith(stex.pac.upper()):
        return None  # a path that is not the one of this slot's STEX: not ready
    if stex is None and find_variant(profile, slot, file) is None and not accept_stex:
        return None  # nothing to check the path against yet
    return file


# -- presets -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetRef:
    """What the preset selection needs from one row of the Stages list."""

    key: str
    match_variants: bool
    enabled: bool = True


def select_preset(entries: Sequence[PresetRef], key: str | None) -> int | None:
    """Index of the row for the stage ``key``: an exact key first, else a row of the same slot with
    "match variants". None when nothing matches (or no stage)."""
    if not key:
        return None
    want = split_key(key)
    if want is None:
        return None
    for i, e in enumerate(entries):
        if e.enabled and e.key.upper() == key.upper():
            return i
    for i, e in enumerate(entries):
        got = split_key(e.key)
        if e.enabled and e.match_variants and got is not None and got[0] == want[0]:
            return i
    return None


@dataclass(frozen=True)
class StageOverrides:
    """What a row of the Stages list may override for its stage, or the global settings.

    ``near_object`` / ``far_object``: any object (opaque here), None = not set; ``pause_frame``:
    a game frame (frames since GO), 0 = off.
    """

    near_object: object | None = None
    far_object: object | None = None
    pause_frame: int = 0


def effective_overrides(row: StageOverrides | None, glob: StageOverrides) -> StageOverrides:
    """The settings in force: each value of the current stage's row when set, else the global one.

    ``row`` None (no row for the stage, presets off): the global settings as they are."""
    if row is None:
        return glob
    return StageOverrides(
        row.near_object if row.near_object is not None else glob.near_object,
        row.far_object if row.far_object is not None else glob.far_object,
        row.pause_frame if row.pause_frame > 0 else glob.pause_frame,
    )


# -- stage camera views (stage-identity.md 3.3 to 3.5) --------------------------------------------


@dataclass(frozen=True)
class StageCamera:
    """What the views need, read in a match (stage block names: semantic)."""

    range_abs: tuple[float, float, float, float]  # left, right, top, bottom (CameraController+0x148)
    params: dict  # cmStageParam with semantic names (pitch_factor, yaw_factor, ...)
    fixed: tuple[float, ...]  # STPM +0x8C..+0xA0: eye x, y, distance, FOV deg, h angle, v angle (deg)
    constants: tuple[float, ...]  # cmAIController +0xB8..+0xDC
    aspect: float
    widescreen: bool


# Stage block (+0x00..+0x3C of [g_Stage+0x78]): +0x18 = pitch factor, +0x1C = yaw factor (the
# controller's copy swaps them, stage-identity.md 3.1).
STAGE_PARAM_FIELDS = (
    "range_left",
    "range_right",
    "range_top",
    "range_bottom",
    "min_distance",
    "max_distance",
    "pitch_factor",
    "yaw_factor",
    "center_x",
    "center_y",
    "center_z",
    "tilt",
    "fov",
    "speed",
    "bubble",
    "unk_3c",
)


def read_stage_camera(game: Game) -> StageCamera | None:
    p = game.p
    stage = game.stage_pointer()
    if stage is None:
        return None
    param = game.deref(stage + p.STAGE_CAMERA_PARAM.offset)
    stpm = game.deref(stage + p.STAGE_STPM.offset)
    if param is None or stpm is None:
        return None
    raw = game.read_bytes(param, 0x40)
    fixed = game.read_bytes(stpm + p.STPM_FIXED.offset, p.STPM_FIXED.size)
    if raw is None or fixed is None:
        return None
    params = dict(zip(STAGE_PARAM_FIELDS, struct.unpack(">16f", raw), strict=True))
    cx, cy = params["center_x"], params["center_y"]
    rng = (params["range_left"] + cx, params["range_right"] + cx, params["range_top"] + cy, params["range_bottom"] + cy)
    constants = p.AI_CONSTANTS_DEFAULT
    cc = game.deref(p.CAMERA_CONTROLLER.addr)
    ai = None if cc is None else game.deref(cc + p.CC_AI_CONTROLLER.offset)
    if ai is not None and game.read_u32(ai + p.AI_CAMERA.offset) == p.CAM_RECORD_BASE:
        data = game.read_bytes(ai + p.AI_CONSTANTS.offset, p.AI_CONSTANTS.size)
        if data is not None:
            values = struct.unpack(p.AI_CONSTANTS.fmt, data)
            if all(math.isfinite(v) for v in values):
                constants = values
    aspect = game.read_value(p.CAM_ASPECT)
    if aspect is None or not math.isfinite(aspect) or not 0.5 < aspect < 4.0:
        aspect = 1.7323
    widescreen = True
    gg = game.deref(p.GAME_GLOBAL.addr)
    rec = None if gg is None else game.deref(gg + p.GG_RECORD.offset)
    if rec is not None:
        flag = game.read_bytes(rec + p.RECORD_WIDESCREEN.offset, 1)
        if flag is not None:
            widescreen = bool(flag[0] & 0x80)
    return StageCamera(rng, params, struct.unpack(p.STPM_FIXED.fmt, fixed), tuple(constants), aspect, widescreen)


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _frame_rect(rect, params, constants, fov_deg, aspect, rng, hfac, vfac):
    """fn_800A1B50 (stage_probe.frame_rect): eye/target goals that frame ``rect``."""
    b8, bc, c0, c4, c8, cc, d0, d4, d8, dc = constants
    xmin, ymin, xmax, ymax = rect
    m = max(xmax - xmin, ymax - ymin)
    k = d4 if m >= dc else d0 if m <= d8 else d0 + (d4 - d0) * (m - d8) / (dc - d8)
    cy = params["center_y"]
    v = -(math.radians(params["pitch_factor"]) * (cy + (ymin + ymax - 2 * cy) * (0.5 - k) + bc))
    pitch = params["tilt"] + _clamp(v, math.radians(c4), math.radians(c0))
    yaw = _clamp(
        -(math.radians(params["yaw_factor"]) * (0.5 * (xmin + xmax) - params["center_x"])),
        math.radians(cc),
        math.radians(c8),
    )
    hf = 0.5 * math.radians(fov_deg)
    up, dn = vfac * math.tan(hf + pitch), vfac * math.tan(hf - pitch)
    dv = (ymax - ymin) / (up + dn)
    d_range = (rng[2] - rng[3]) / (up + dn)
    ty = ymax - dv * up + vfac * dv * math.tan(pitch)
    dy = vfac * dv * math.tan(pitch)
    lh, rh = hfac * aspect * math.tan(hf - yaw), hfac * aspect * math.tan(hf + yaw)
    dh = (xmax - xmin) / (lh + rh)
    dx = hfac * aspect * dh * math.tan(yaw)
    tx = xmax - dh * lh - dx
    d = min(max(max(dv, dh), max(params["min_distance"], b8)), params["max_distance"])
    if dv < d:
        dy = vfac * min(d, d_range) * math.tan(pitch)
    return [tx, ty, 0.0], [tx + dx, ty - dy, d]


def _rotx(v, a):
    c, s = math.cos(a), math.sin(a)
    return (v[0], c * v[1] - s * v[2], s * v[1] + c * v[2])


def _roty(v, a):
    c, s = math.cos(a), math.sin(a)
    return (c * v[0] + s * v[2], v[1], -s * v[0] + c * v[2])


def _footprint_clamp(eye, target, fov_rad, aspect, hfac, vfac, rng):
    """fn_800A1E94 (stage_probe.footprint_clamp): keep the view footprint on z = 0 inside ``rng``."""
    v = [target[i] - eye[i] for i in range(3)]
    if v[0] ** 2 + v[1] ** 2 + v[2] ** 2 <= 1.1920929e-07:
        return eye, target
    pitch, yaw = math.atan2(v[1], -v[2]), math.atan2(-v[0], -v[2])
    if abs(pitch) > 0.785398 or abs(yaw) > 0.785398:
        return eye, target
    t = math.tan(0.5 * fov_rad)
    fx, fy = t * aspect * hfac, t * vfac
    xs, ys = [], []
    for corner in ((-fx, fy, -1.0), (fx, fy, -1.0), (-fx, -fy, -1.0), (fx, -fy, -1.0)):
        c = _roty(_rotx(corner, pitch), yaw)
        if c[2] > -0.0001:
            return eye, target
        k = -eye[2] / c[2]
        xs.append(c[0] * k + eye[0])
        ys.append(c[1] * k + eye[1])
    left, right, top, bottom = rng

    def shift(lo_v, hi_v, lo_r, hi_r):
        a, b = lo_r - lo_v, hi_r - hi_v
        if hi_v - lo_v >= hi_r - lo_r:
            return 0.5 * (a + b)
        if a > 0:
            return a
        if b < 0:
            return b
        return 0.0

    sx = shift(min(xs), max(xs), left, right)
    sy = shift(min(ys), max(ys), bottom, top)
    return [eye[0] + sx, eye[1] + sy, eye[2]], [target[0] + sx, target[1] + sy, target[2]]


View = tuple[tuple[float, float, float], tuple[float, float, float], float]  # Brawl eye, target, FOV rad


def wide_view(cam: StageCamera) -> View:
    """The whole camera range framed by the match camera's own rules (stage-identity.md 3.5: exact)."""
    hfac, vfac = (0.875, 1.0) if cam.widescreen else (1.0, 0.857142865657806)
    left, right, top, bottom = cam.range_abs
    fov_deg = math.degrees(cam.params["fov"])
    target, eye = _frame_rect(
        (left, bottom, right, top), cam.params, cam.constants, fov_deg, cam.aspect, cam.range_abs, hfac, vfac
    )
    eye, target = _footprint_clamp(eye, target, cam.params["fov"], cam.aspect, hfac, vfac, cam.range_abs)
    return (eye[0], eye[1], eye[2]), (target[0], target[1], target[2]), cam.params["fov"]


def fixed_view(cam: StageCamera) -> View:
    """The stage's fixed camera (STPM, stage-identity.md 3.4): the 'centre' is the eye."""
    cx, cy, d, fov_deg, h, v = cam.fixed
    eye = (cx, cy, d)
    target = (cx - d * math.tan(math.radians(h)), cy + d * math.tan(math.radians(v)), 0.0)
    return eye, target, math.radians(fov_deg)


# -- reload on another stage (stage-reload.md 4) --------------------------------------------------

RELOAD_READY = "ready"
RELOAD_WAIT = "wait"  # intro 3-2-1: queued until the fight runs
RELOAD_REFUSED = "refused"


@dataclass
class ReloadContext:
    scene: int
    sequence: int
    mode_melee: int
    loader: int
    flags: int
    slot: int


def match_is_vs(game: Game) -> bool | None:
    """scMelee inside sqVsMelee (the only case Reload on stage supports). Read once per match."""
    scene, seq = scene_pointers(game)
    if scene is None or seq is None:
        return None
    return read_cstr(game, scene) == game.p.SCENE_MATCH and read_cstr(game, seq) == game.p.SEQUENCE_VS


def reload_check(game: Game) -> tuple[str, ReloadContext | str]:
    """(RELOAD_READY, context) during the fight of a VS match; (RELOAD_WAIT, why) during the intro;
    (RELOAD_REFUSED, why) anywhere else. Pure reads."""
    p = game.p
    if not game.connected:
        return RELOAD_REFUSED, "not connected"
    scene, seq = scene_pointers(game)
    scene_name = read_cstr(game, scene) if scene is not None else None
    seq_name = read_cstr(game, seq) if seq is not None else None
    if scene is None or seq is None or scene_name != p.SCENE_MATCH or seq_name != p.SEQUENCE_VS:
        return RELOAD_REFUSED, (
            f"only in a VS match (scene {scene_name or '?'}, sequence {seq_name or '?'}): not in Training, "
            "replays, menus or other modes"
        )
    step = game.read_value_at(seq + p.SEQ_STEP.offset, p.SEQ_STEP.fmt)
    line = game.read_value_at(scene + p.SC_COROUTINE.offset, p.SC_COROUTINE.fmt)
    flags = game.read_value_at(scene + p.SC_FLAGS.offset, p.SC_FLAGS.fmt)
    if step is None or line is None or flags is None:
        return RELOAD_REFUSED, "match state unreadable"
    if step != p.SEQ_STEP_MATCH:
        return RELOAD_REFUSED, f"match sequence step {step} (a reload is already under way?)"
    if line != p.SC_RUNNING_LINE:
        frame = game.current_frame()
        if line < p.SC_RUNNING_LINE and not frame:
            return RELOAD_WAIT, "the stage intro is playing: queued until the fight starts"
        return RELOAD_REFUSED, f"the match is ending (state {line})"
    if flags & (p.SC_FLAG_RUNNING | p.SC_FLAG_ENDING) != p.SC_FLAG_RUNNING:
        return RELOAD_REFUSED, f"the match is ending (flags 0x{flags:02X})"
    mm = mode_melee(game)
    loader = game.deref(scene + p.SC_LOADER.offset)
    if mm is None or loader is None:
        return RELOAD_REFUSED, "stage loader not found"
    if game.read_u32(loader + p.LD_MODE_MELEE.offset) != mm:
        return RELOAD_REFUSED, "stage loader does not belong to this match"
    state = game.read_value_at(loader + p.LD_STATE.offset, p.LD_STATE.fmt)
    path = game.read_bytes(loader + p.LOADER_PATH.offset, p.LOADER_PATH.size)
    slot = game.read_value_at(mm + p.MM_STAGE_SLOT.offset, p.MM_STAGE_SLOT.fmt)
    loaded = game.read_u32(loader + p.LD_SLOT.offset)
    if state != p.LD_STATE_WATCH or path is None or not _cstr(path).upper().startswith("/STAGE/"):
        return RELOAD_REFUSED, f"stage loader in an unexpected state ({state})"
    if slot is None or loaded != slot:
        return RELOAD_REFUSED, "stage loader slot differs from the match slot"
    return RELOAD_READY, ReloadContext(scene, seq, mm, loader, flags, slot)


def reload_write(game: Game, ctx: ReloadContext, slot: int, asl: int = 0) -> bool:
    """The 8 writes of stage-reload.md 4.2, in that order (1 before 2, 3 before 4, 8 last).

    The flags byte is read again (fresh) right before the last write.
    """
    p = game.p
    ok = game.write_bytes(ctx.loader + p.LD_STATE.offset, bytes([p.LD_STATE_IDLE]))  # 1. loader stops watching
    ok = ok and game.write_bytes(ctx.mode_melee + p.MM_STAGE_SLOT.offset, struct.pack(">H", slot))  # 2. new slot
    ok = ok and game.write_bytes(ctx.loader + p.LD_SLOT.offset, struct.pack(">I", slot))  # 3. "already loaded"
    ok = ok and game.write_bytes(ctx.loader + p.LD_STATE.offset, bytes([p.LD_STATE_WATCH]))  # 4. watch again
    if not ok:
        return False
    ok = game.write_field(p.ASL_BUTTONS, asl & 0xFFFF)  # 5. variant
    ok = game.write_field(p.PP_LAST_SLOT, 0xFFFF) and ok  # 6. P+ reads .asl/.param again
    ok = game.write_bytes(ctx.sequence + p.SEQ_STEP.offset, struct.pack(">I", p.SEQ_STEP_REPLAY)) and ok  # 7.
    if not ok:
        return False
    flags = game.read_bytes(ctx.scene + p.SC_FLAGS.offset, 1, fresh=True)
    if flags is None:
        return False
    return game.write_bytes(ctx.scene + p.SC_FLAGS.offset, bytes([flags[0] & ~p.SC_FLAG_RUNNING & 0xFF]))  # 8.


def reload_progress(game: Game, slot: int) -> bool:
    """True once the new match runs: scMelee, sequence step 10, g_Stage set, the requested slot."""
    p = game.p
    scene, seq = scene_pointers(game)
    if scene is None or seq is None or read_cstr(game, scene) != p.SCENE_MATCH:
        return False
    if game.read_value_at(seq + p.SEQ_STEP.offset, p.SEQ_STEP.fmt) != p.SEQ_STEP_MATCH:
        return False
    if game.stage_pointer() is None:
        return False
    mm = mode_melee(game)
    return mm is not None and game.read_value_at(mm + p.MM_STAGE_SLOT.offset, p.MM_STAGE_SLOT.fmt) == slot
