"""Every game address used by EBC, in one place.

Each ``Field`` carries its absolute guest address, its ``struct`` format
(always big-endian, the Wii is big-endian) and a note on what is known about
it. Status words used in the notes:

* ``verified``   measured in RAM on Project+ 3.1.5 (write, read back and/or
                 screenshot), see docs/research/addresses-pplus-3.1.5.md and
                 docs/research/per-fighter-color.md;
* ``deduced``    read in the game code (disassembly, BrawlHeaders), consistent
                 with the measurements, but that exact case was not played;
* ``empirical``  inherited from EBC v1/v2, works in practice, not understood.

Two profiles: ``pplus-3.1.5`` and ``pplus-3.2`` (RSBE01 + Project+ vanilla).
None of these addresses is in the DOL: they live in heaps that are created at
fixed addresses, so they are deterministic for one build. A profile subclasses
the previous one and only redefines what moved (docs/research/addresses-pplus-3.2.md).
``detect_profile`` picks the profile from the Gecko codesets resident in RAM.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

MEM1_START = 0x80000000
MEM1_END = 0x81800000  # exclusive (24 MiB)
MEM2_START = 0x90000000
MEM2_END = 0x94000000  # exclusive (64 MiB)


def is_valid_pointer(addr: int) -> bool:
    """True if ``addr`` points inside MEM1 or MEM2 (and so may be dereferenced)."""
    return MEM1_START <= addr < MEM1_END or MEM2_START <= addr < MEM2_END


@dataclass(frozen=True)
class Field:
    name: str
    addr: int
    fmt: str
    doc: str = ""

    @property
    def size(self) -> int:
        return struct.calcsize(self.fmt)

    def pack(self, *values: float | int) -> bytes:
        return struct.pack(self.fmt, *values)

    def unpack(self, data: bytes) -> tuple:
        return struct.unpack(self.fmt, data)


@dataclass(frozen=True)
class Offset:
    """Offset inside a dynamically allocated structure (resolved at run time)."""

    name: str
    offset: int
    fmt: str = ">I"
    doc: str = ""

    @property
    def size(self) -> int:
        return struct.calcsize(self.fmt)


class PPlus315:
    """Super Smash Bros. Brawl (RSBE01) + Project+ 3.1.5 (vanilla)."""

    name = "pplus-3.1.5"
    label = "Project+ 3.1.5"
    # Version signature: the two Gecko codesets (BOOST.GCT, then RSBE01.GCT, NETBOOST /
    # NETPLAY.GCT with the netplay launcher) are copied verbatim from the SD card, at fixed
    # addresses; their end marker sits at a place set by the file size. Each variant lists
    # the end-marker addresses that must all hold CODESET_END (addresses-pplus-3.2.md 4).
    CODESETS = (0x80550010, 0x80566528)
    SIGNATURES: tuple[tuple[str, tuple[int, ...]], ...] = (
        ("offline", (0x80550010 + 0xF4D0 - 8, 0x80566528 + 0x181F0 - 8)),  # verified in RAM
        ("netplay", (0x80550010 + 0xF510 - 8, 0x80566528 + 0x18318 - 8)),  # verified in RAM
    )

    # -- Match -----------------------------------------------------------------
    MATCH_FRAME = Field(
        "match_frame",
        0x8062B420,
        ">I",
        "verified: game frames since GO. 0 in menus, during the intro and on the results screen; frozen in pause.",
    )
    MATCH_ENTRY_TIMER = Field(
        "match_entry_timer",
        0x805BBFF0,
        ">H",
        "verified: frames since the match was loaded (= MATCH_FRAME + ~213). UNSIGNED: goes past 32767 after "
        "~9 min (v2 read it signed). Keeps its last value outside a match.",
    )
    REPLAY_MODE = Field(
        "replay_mode",
        0x805BBFC0,
        ">I",
        "verified: 0 = no match, 2 = recording (every VS match), 1 = replay playback. Rising edge = new match.",
    )
    G_STAGE = Field(
        "g_stage",
        0x80B8A428,
        ">I",
        "verified: Stage* of the current stage (sora_melee .bss). NULL on CSS/SSS, stale on the results screen.",
    )
    STAGE_ID = Field("stage_id", 0x8062B3B4, ">I", "verified: stage id (PS2 = 46, FD = 2). Display only.")
    # Stages where some floor decals are not drawn in orthographic (ortho-projection.md 3.2): PS2.
    ORTHO_DECAL_STAGES = (46,)
    # [[g_Stage] + 0x78] = cmStageParam (0x40 bytes); +0x30 = stage FOV (rad).
    STAGE_CAMERA_PARAM = Offset("stage.camera_param", 0x78, ">I", "verified: cmStageParam* (changes with the stage)")
    STAGE_PARAM_FOV = Offset(
        "camera_param.fov",
        0x30,
        ">f",
        "verified: stage FOV target in radians. The camera controller eases CAM_FOV towards it every frame: "
        "writing only CAM_FOV gives a 10 % bias and the 'shake'. Write both.",
    )

    # -- Camera (gfCamera 0 at 0x805B6D20) --------------------------------------
    CAM_CONTROLLER_KIND = Field(
        "cam_controller_kind",
        0x80663E40,
        ">I",
        "verified: kind of the current camera controller. 2 = match, 7 = pause (photo), 8 = menus.",
    )
    CAM_KIND_MATCH = 2
    CAM_KIND_PAUSE = 7
    CAM_TYPE = Field(
        "cam_type",
        0x806155C9,
        ">B",
        "verified: 0 = in game, 1 = pause, but stays 1 in the menus after quitting from pause. Do not use alone.",
    )
    CAM_FOV = Field(
        "cam_fov",
        0x805B6DF0,
        ">f",
        "verified: current FULL VERTICAL FOV in RADIANS (setGX -> C_MTXPerspective). Default 0.5236 (30 deg). "
        "Rewritten every frame from the stage FOV, see STAGE_PARAM_FOV and CAM_FOV_COPY. Never write +0xD8 "
        "(0x805B6DF8, CAM_FOV_EFFECTIVE).",
    )
    CAM_FOV_EFFECTIVE = Field(
        "cam_fov_effective",
        0x805B6DF8,
        ">f",
        "verified (camera-motion.md 8a): FOV handed to C_MTXPerspective (vertical, rad), after easing and "
        "zoom. READ ONLY: recorded by Brawl -> Blender and Record, never written.",
    )
    CAM_FOV_COPY = Field(
        "cam_fov_copy",
        0x80663EB4,
        ">f",
        "verified (camera-motion.md 1.3/5): CameraController+0xB4, the stage FOV copied every frame before the "
        "frame counter moves; the easing goal of the camera FOV. Writing stage + this + CAM_FOV cuts the FOV "
        "on the very next frame (50.000 deg measured), without it the first frame is 10 % off.",
    )
    CAM_ASPECT = Field("cam_aspect", 0x805B6E04, ">f", "verified: projection aspect (width/height), 1.7323 (834x480).")
    CAM_WORLD_MATRIX = Field(
        "cam_world_matrix",
        0x805B6D50,
        ">12f",
        "verified (camera-motion.md 1.2/8a): gfCamera+0x30, camera -> world 3x4 (rows, Brawl), inverse of the "
        "view matrix, screen-shake angles included. Rebuilt every frame.",
    )
    CAM_ROT_XY = Field(
        "cam_rot_xy",
        0x805B6DE0,
        ">2f",
        "verified (camera-motion.md 4): gfCamera pitch = asin(d.y), yaw = atan2(-d.x, -d.z), d = target - eye. "
        "The pause controller reads them back as its current angles: written with the pause block for a cut.",
    )
    # gfCamera +0x00..+0x104 in one read (Record, Brawl -> Blender): world matrix, target, eye, roll, fov_gx,
    # and the projection (ortho bounds + type).
    CAM_RECORD_BASE = 0x805B6D20
    CAM_RECORD_SIZE = 0x104
    CAM_ROT_Z = Field("cam_rot_z", 0x805B6DE8, ">f", "verified: camera roll in radians, never rewritten by the game.")
    CAM_FRONT_DEPTH = Field("cam_front_depth", 0x805B6DFC, ">f", "verified: near plane, default 1, never rewritten.")
    CAM_BACK_DEPTH = Field("cam_back_depth", 0x805B6E00, ">f", "verified: far plane, default 1e6, never rewritten.")
    CAM_ORTHO_BOUNDS = Field(
        "cam_ortho_bounds",
        0x805B6E08,
        ">4f",
        "verified (ortho-projection.md 1.2, 4.1): gfCamera+0xE8 top, bottom, left, right (Brawl units, view "
        "space), used by setGX (C_MTXOrtho) and the g3d camera (SetOrtho) when CAM_PROJECTION is 0. Default "
        "0/480/0/640, never rewritten in a match (animated cameras fn_800194AC load their own).",
    )
    CAM_PROJECTION = Field(
        "cam_projection",
        0x805B6E20,
        ">I",
        "verified (ortho-projection.md 1.1, 3.3): gfCamera+0x100, 1 = perspective (default), 0 = orthographic. "
        "Read by gfCamera::setGX every frame; pure data, no effect on positions nor RNG (295/295 frames).",
    )
    CAM_SHAKE = Field(
        "cam_shake",
        0x805B6DCC,
        ">3f",
        "verified: view offset X Y Z used by the screen shake (quake), v2's ABS_ORG. (0, 0, 0) = no shake.",
    )
    # In-game camera: target (look-at point) then eye, 3 floats each,
    # contiguous, in Brawl space (x right, y up, z towards the viewer).
    CAM_IG_ORIGIN = Field(
        "cam_ig_origin",
        0x805B6D80,
        ">3f",
        "verified: target; X/Y kept only with the camera lock, Z (Brawl) reset to 0 by the game every frame, "
        "lock or not (camera-motion.md 1.1): the target must lie on the stage plane.",
    )
    CAM_IG_POSITION = Field("cam_ig_position", 0x805B6D8C, ">3f", "verified: eye; kept only with the camera lock.")
    CAM_IG_BLOCK = Field("cam_ig_block", 0x805B6D80, ">6f", "CAM_IG_ORIGIN + CAM_IG_POSITION in one write.")
    # Pause camera (cmPhotoController at 0x806636A0). Reset on every pause entry.
    CAM_PAUSE_OFFSET = Field(
        "cam_pause_offset",
        0x806636C8,
        ">3f",
        "verified: target OFFSET from the pause subject (not an absolute origin). (0, 1, 0) on pause entry.",
    )
    CAM_PAUSE_ANGLES = Field("cam_pause_angles", 0x806636D4, ">2f", "verified: pitch, yaw in radians (-0.2269, 0).")
    CAM_PAUSE_DISTANCE = Field("cam_pause_distance", 0x806636DC, ">f", "verified: eye-target distance (40).")
    CAM_PAUSE_BLOCK = Field("cam_pause_block", 0x806636C8, ">6f", "offset(3) + pitch + yaw + distance in one write.")
    CAM_PAUSE_FOCUS = Field(
        "cam_pause_focus", 0x806636E0, ">i", "verified: pause subject, -1 = stage, else 0-based port. An int."
    )
    CAM_PAUSE_SUBJECT = Field(
        "cam_pause_subject",
        0x806636E4,
        ">2f",
        "verified: subject X, Y written by the game (fighter + (0, 10)); rendered target = subject + offset.",
    )

    # -- Native camera freeze (docs/research/camera-freeze.md, recipe R2) -------
    CAMERA_CONTROLLER = Field(
        "camera_controller",
        0x805A0278,
        ">I",
        "verified (camera-freeze.md 2): CameraController* (gfTask, 0x80663E00).",
    )
    CC_AI_CONTROLLER = Offset(
        "camera_controller.ai", 0x58, ">I", "verified (camera-freeze.md 4.2): cmAIController* (kind 2), 0x80663B40"
    )
    AI_CAMERA = Offset("ai.camera", 0x00, ">I", "verified: the gfCamera* it drives, 0x805B6D20: guard")
    AI_FLAGS = Offset(
        "ai.flags",
        0x94,
        ">B",
        "verified (camera-freeze.md 3, 4.2): bit 0x10 = update active (0x19 in a match). Cleared: the match "
        "camera, its quake and the subject camera stop, and gfCamera::update is no longer called: EBC writes "
        "the whole derived block (CAM_BLOCK_SIZE). Lost when the controller is rebuilt: re-assert every tick.",
    )
    AI_UPDATE_BIT = 0x10
    # gfCamera +0x00..+0x114: what gfCamera::update derives (+ near, far, aspect, viewport, flags kept).
    CAM_BLOCK_SIZE = 0x114

    # -- Code Menu (u32 values, written as their low byte) ---------------------
    DEBUG_MENU = Field("debug_menu", 0x804E0D33, ">B", "verified layout (low byte of u32 0x804E0D30); effect untested.")
    DISPLAY_HURTBOX = Field(
        "display_hurtbox",
        0x804E0D5F,
        ">B",
        "verified: low byte of u32 0x804E0D5C. 0 = off, 1 = hurtboxes, 2 = hurtboxes + models hidden. "
        "v2 wrote 0x804E0D6B (the entry's 'max' field): no effect.",
    )
    DISPLAY_STAGE_COLLISION = Field(
        "display_stage_collision",
        0x804E0DE3,
        ">B",
        "verified: low byte of u32 0x804E0DE0. 0 = off, 1 = collisions, 2 = collisions + background hidden. "
        "v2 wrote 0x804E0DEF ('max' field): no effect.",
    )
    CAM_LOCK = Field("cam_lock", 0x804E0E37, ">B", "verified: camera lock (low byte of u32 0x804E0E34).")
    CAM_LOCK_GAME = Field(
        "cam_lock_game",
        0x80583FFA,
        ">H",
        "verified (camera-motion.md 2): the u16 the P+ lock hooks test, copied from CAM_LOCK once per frame, too "
        "late for the current one. Written with CAM_LOCK so the lock holds from the next frame on.",
    )
    DRAW_DI = Field("draw_di", 0x804E0E67, ">B", "verified layout (low byte of u32 0x804E0E64).")
    DISPLAY_HUD = Field("display_hud", 0x804E0EC3, ">B", "verified: 1 = shown (default), 0 = hidden (+ P1/P2 tags).")
    # Every Code Menu value persists across results/CSS/new match.

    # -- Scene visibility (gfSceneRoot records, bit 0x80 = draw the group) -----
    DISPLAY_STAGE = Field(
        "display_stage",
        0x806732D9,
        ">B",
        "verified: flags byte of the stage group, 0xAC visible, 0x2C hidden. Flip bit 0x80 only "
        "(v2 wrote the u32 0xAC001500 there and corrupted the group index).",
    )
    DISPLAY_CHAR_MODEL = Field(
        "display_char_model",
        0x806732F1,
        ">B",
        "verified: flags byte of the fighters group, 0x8C visible, 0x0C hidden. Flip bit 0x80 only "
        "(v2 wrote 0 into the group index at 0x806732F3).",
    )
    VISIBLE_BIT = 0x80
    # Both persist across matches (gfSceneRoot survives): restore on exit.

    # -- Audio (MEM2) ------------------------------------------------------------
    # [SOUND_SETTINGS] = the music sound object, whose +0x80 is the volume that the P+
    # hook at 0x801BCE60 reads every frame. Same root on 3.1.5 and 3.2, the object moved
    # (0x90345698 -> 0x90342E78): follow the chain, guarded by the object's back pointer.
    SOUND_SETTINGS = Field(
        "sound_settings",
        0x90E60F00,
        ">I",
        "verified (addresses-pplus-3.2.md 3): music sound object*. Same address on 3.1.5 and 3.2.",
    )
    BGM_BACKREF = Offset("bgm.settings", 0x08, ">I", "verified: points back to SOUND_SETTINGS (guard)")
    BGM_VOLUME = Offset("bgm.volume", 0x80, ">f", "verified persistence: 1.0, reset on return to the CSS.")
    # Fixed address of the music volume when the chain cannot be followed (3.1.5 only).
    MUSIC_VOLUME: Field | None = Field(
        "music_volume", 0x90345718, ">f", "verified on 3.1.5: [SOUND_SETTINGS] + BGM_VOLUME, fixed in practice."
    )
    SFX_VOLUME = Field("sfx_volume", 0x90E60F38, ">f", "verified persistence: 1.0, never reset by the game.")

    # -- Shadows (gfSceneRoot) --------------------------------------------------
    SHADOW_X = Field("shadow_x", 0x806733BC, ">f", "verified: m_shadowPitch in DEGREES, reset on every stage load.")
    SHADOW_Y = Field("shadow_y", 0x806733C0, ">f", "verified: m_shadowYaw in DEGREES, reset on every stage load.")

    # -- Background -------------------------------------------------------------
    BACKGROUND_COLOR = Field(
        "background_color",
        0x805B505C,
        ">4B",
        "verified: RGBA EFB clear colour, 00000000 in a match. Visible wherever nothing is drawn: the 'void' "
        "around the stage, the whole background once the stage is hidden. Menus overwrite then restore it.",
    )
    # The clear colour's alpha has no visible effect (green-screen-alpha.md 3): only RGB matters.

    # -- Full-screen fill layer: the game's efScreen (docs/research/green-screen-alpha.md) ---
    EFSCREEN_PTR = Field(
        "efscreen_ptr",
        0x805A0140,
        ">I",
        "verified (green-screen-alpha.md 1.1): g_efScreen (.sbss of the DOL) -> efScreen, 0x80663060 on every "
        "measure (PS2, FD, training, replay, new match after the CSS). Guard: MEM1.",
    )
    EFSCREEN_LAYER0 = Offset(
        "efscreen.layer0",
        0x00,
        ">17B",
        "verified (green-screen-alpha.md 1.2): layer 0 (drawn after the stage pass, under the fighters): "
        "count, then 16 s8 slots holding request numbers, -1 (0xFF) = free. Emptied when a match ends.",
    )
    EFSCREEN_REQUESTS = Offset("efscreen.requests", 0x9C, ">32B", "verified: request i at +0x9C + 0x20 * i, i = 0..9")
    EFSCREEN_REQUEST_STRIDE = 0x20
    # Request 9, the last: requestFill allocates from 0, a Final Smash never takes it.
    EFSCREEN_REQUEST_INDEX = 9
    EFSCREEN_SLOTS = 16
    REQ_ACTIVE = Offset("request.active", 0x01, ">B", "verified: 0 = free. Cleared when a match ends.")
    REQ_TYPE = Offset("request.type", 0x05, ">B", "verified: 2 = flat fill (0 = pulse, others draw nothing)")
    REQ_COLORS = Offset(
        "request.colors",
        0x14,
        ">12B",
        "verified: RGBA start, target, current (the drawn one). Alpha is exact: src * A + dst * (1 - A).",
    )

    # -- Fighters (dynamic structures) -----------------------------------------
    # ftEntryManager is the OBJECT itself: its first word points to the entry
    # array ([0x80624780] == 0x806232F0). Entries are in creation order, NOT
    # by port: the port is read from each entry.
    FT_ENTRY_MANAGER = Field("ft_entry_manager", 0x80624780, ">I", "verified")
    FT_ENTRY_COUNT = 4
    FT_ENTRY_STRIDE = 0x244
    ENTRY_INSTANCE_COUNT = Offset("entry.instance_count", 0x08, ">B", "verified: 1, 2 (Zelda/Sheik), 0 empty")
    ENTRY_ACTIVE_INSTANCE = Offset("entry.active_instance", 0x0A, ">b", "verified: 0 -> 1 after Zelda->Sheik")
    ENTRY_PORT = Offset("entry.port", 0x18, ">i", "verified: m_slotIndex = port - 1, -1 = empty entry")
    ENTRY_INSTANCES = Offset("entry.instances", 0x30, ">iI", "verified: 8 bytes each: ftKind, Fighter*")
    ENTRY_INSTANCE_STRIDE = 8
    ENTRY_MAX_INSTANCES = 3  # Pokemon Trainer (deduced)
    FIGHTER_MODULES = Offset("fighter.modules", 0x60, ">I", "verified: soModuleAccesser*")
    MODULES_POSTURE = Offset("modules.posture", 0x18, ">I", "verified: soPostureModule*")
    POSTURE_POSITION = Offset("posture.position", 0x0C, ">3f", "verified: ground position X Y Z (Z ~ 0)")
    MODULES_VISIBILITY = Offset("modules.visibility", 0x64, ">I", "verified: soVisibilityModule*")
    VIS_ACCESSER = Offset("vis.accesser", 0x10, ">I", "verified: guard, points back to the soModuleAccesser")
    VIS_VISIBLE = Offset(
        "vis.visible", 0x0E, ">B", "verified: 1 drawn, 0 hidden. Respawn sets it back to 1: re-assert every tick."
    )
    MODULES_COLOR_BLEND = Offset("modules.color_blend", 0xB8, ">I", "verified: soColorBlendModule*")
    CBM_ACCESSER = Offset("cbm.accesser", 0x2C, ">I", "verified: guard, points back to the soModuleAccesser")
    CBM_SUB_COLOR = Offset("cbm.sub_color", 0x14A, ">4B", "verified: sub colour RGBA (alpha 255 = flat colour)")
    CBM_SUB_ENABLE = Offset(
        "cbm.sub_enable", 0x14F, ">B", "verified: sub colour enable. A KO/respawn resets the channel to 0."
    )
    # ftKind values with special handling.
    KIND_POPO = 0x0F
    KIND_NANA = 0x10  # deduced: Ice Climbers = two active instances in one entry


class PPlus32(PPlus315):
    """Super Smash Bros. Brawl (RSBE01) + Project+ 3.2 (vanilla).

    Everything EBC uses measured identical to 3.1.5 (docs/research/addresses-pplus-3.2.md)
    except the MEM2 heap layout (the music sound object moved) and the codesets.
    """

    name = "pplus-3.2"
    label = "Project+ 3.2"
    SIGNATURES = (
        ("offline", (0x80550010 + 0xF6A8 - 8, 0x80566528 + 0x18CC8 - 8)),  # verified in RAM
        ("netplay", (0x80550010 + 0xF6E8 - 8, 0x80566528 + 0x18DF8 - 8)),  # verified in RAM
    )
    MUSIC_VOLUME = None  # 0x90345718 is unused heap (0xCCCCCCCC) on 3.2: chain only


PROFILES: dict[str, type[PPlus315]] = {p.name: p for p in (PPlus315, PPlus32)}
DEFAULT_PROFILE = PPlus315

CODESET_HEADER = bytes.fromhex("00D0C0DE00D0C0DE")
CODESET_END = bytes.fromhex("F000000000000000")


def detect_profile(read: Callable[[int, int], bytes | None]) -> tuple[type[PPlus315] | None, str]:
    """Identify the Project+ build from the resident codesets.

    ``read(addr, n)`` returns guest bytes or None. Returns ``(profile, variant)`` on a
    match, ``(None, reason)`` otherwise (not P+, codes not loaded yet, modified codes).
    """
    game_id = read(0x80000000, 6)
    if game_id is None:
        return None, "memory unreadable"
    if game_id[1:6] != b"SBE01":
        return None, f"not Brawl RSBE01 (game id {game_id!r})"
    for base in PPlus315.CODESETS:
        if read(base, 8) != CODESET_HEADER:
            return None, "no Project+ codeset in memory (game not booted yet?)"
    for profile in PROFILES.values():
        for variant, ends in profile.SIGNATURES:
            if all(read(a, 8) == CODESET_END for a in ends):
                return profile, variant
    return None, "unknown Project+ build (modified codes?)"
