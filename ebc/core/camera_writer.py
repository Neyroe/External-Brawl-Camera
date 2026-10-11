"""The game camera, written from a Blender pose (docs/research/camera-motion.md,
camera-freeze.md, ortho-projection.md). No ``bpy``.

``Game.camera`` sends one frame of camera in the order of the clean-cut recipe: with the native
freeze, or in pause while the freeze holds the pause camera (pause-camera.md), the whole gfCamera
block in one write; with the Code Menu lock or the unfrozen pause camera, FOV (stage, its per-frame
copy, camera), roll, target + eye (pause: the photo controller's offset / angles / distance),
projection, shake. It also owns the
values that belong to the camera: FOV and roll (overrides restored by ``restore_all``), near/far
clip, and the orthographic projection (the game's perspective words kept aside while EBC holds
the game in orthographic).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .addresses import Field
from .camera import (
    GAME_DEFAULT_ASPECT,
    ORTHO_FAR_MARGIN,
    ORTHO_NEAR,
    PROJECTION_ORTHOGRAPHIC,
    PROJECTION_PERSPECTIVE,
    CameraPose,
    CameraSample,
    look_angles,
    pack_gfcamera_block,
    parse_camera_block,
    resolve_ortho,
    to_blender,
    to_brawl,
    valid_aspect,
)

if TYPE_CHECKING:
    from .game import Game

Vec3 = tuple[float, float, float]
_U32 = struct.Struct(">I")

# Far clip at or above this value is sent as "infinite".
BACK_DEPTH_INFINITE_THRESHOLD = 4999
BACK_DEPTH_INFINITE = 1_000_000.0

# The game's own projection words in a match (ortho-projection.md 1.2): restored when the ones
# found at the first orthographic write are not a perspective camera (a state saved in ortho).
DEFAULT_NEAR_FAR = (1.0, 1_000_000.0)
DEFAULT_ORTHO_BOUNDS = (0.0, 480.0, 0.0, 640.0)


class CameraWriter:
    """``Game.camera``."""

    def __init__(self, game: Game) -> None:
        self.game = game
        self.near: float | None = None  # near / far clip wanted (None = never set)
        self.far: float | None = None
        # projection words as the game had them before the first orthographic write: near/far (8 B),
        # bounds (16 B), type (4 B); None = perspective, nothing of ours in place
        self._projection: tuple[bytes, bytes, bytes] | None = None
        self._block_cache: tuple[tuple, bytes] | None = None  # (inputs, gfCamera block) of the last tick

    def forget(self) -> None:
        """A new connection: nothing of ours is in place."""
        self._projection = None

    def reapply_depth(self) -> None:
        """New match / Re-apply: the near and far clip, when set."""
        if self.near is not None:
            self._apply_depth(self.game.p.CAM_FRONT_DEPTH, self.near)
        if self.far is not None:
            self._apply_depth(self.game.p.CAM_BACK_DEPTH, self.far)

    def kind(self) -> int | None:
        return self.game.read_value(self.game.p.CAM_CONTROLLER_KIND)

    def is_paused(self) -> bool | None:
        """True in pause, False in a match, None elsewhere (menus) or unreadable."""
        kind = self.kind()
        if kind == self.game.p.CAM_KIND_PAUSE:
            return True
        if kind == self.game.p.CAM_KIND_MATCH:
            return False
        return None

    def write_target_eye(self, origin: Sequence[float], position: Sequence[float], paused: bool | None = None) -> bool:
        """Blender-space origin (target) and position (eye) -> in-game or pause camera.

        Pause camera NOT frozen: the game stores the target as an OFFSET from the pause subject
        (a fighter or the stage centre) plus pitch, yaw and distance; the offset is computed from
        the subject position the game publishes. The controller still runs: its FOV is the stage
        pause FOV and the pad moves it too (pause-camera.md 3). Nothing is written in menus.
        """
        if paused is None:
            paused = self.is_paused()
            if paused is None:
                return False
        org = to_brawl(origin)
        pos = to_brawl(position)
        if not paused:
            return self.game.write_field(self.game.p.CAM_IG_BLOCK, *org, *pos, skip_same=True)
        return self._write_pause_block(org, pos)

    def seed_pause_camera(self) -> bool:
        """The pause camera given back in pause: its state set to the camera on screen, so the game
        (and the pad) continue from our camera instead of easing back to the pause entry pose."""
        values = self.game.read_field(self.game.p.CAM_IG_BLOCK)
        if values is None or not all(math.isfinite(v) for v in values):
            return False
        return self._write_pause_block(values[0:3], values[3:6])

    def _write_pause_block(self, org: Sequence[float], pos: Sequence[float]) -> bool:
        """Brawl-space target and eye -> the photo controller's state (offset, angles, distance)."""
        subject = self.game.read_field(self.game.p.CAM_PAUSE_SUBJECT)
        if subject is None or not all(math.isfinite(c) for c in subject):
            return False
        offset = (org[0] - subject[0], org[1] - subject[1], org[2])
        pitch, yaw, distance = look_angles(org, pos)
        ok = self.game.write_field(self.game.p.CAM_PAUSE_BLOCK, *offset, pitch, yaw, distance, skip_same=True)
        # The pause controller reads gfCamera's target and angles back as its current
        # values and eases them at 30 %/frame: writing them too gives a cut on the next
        # frame (camera-motion.md 4). Target = subject + offset = org.
        ok = self.game.write_field(self.game.p.CAM_IG_ORIGIN, *org, skip_same=True) and ok
        return self.game.write_field(self.game.p.CAM_ROT_XY, pitch, yaw, skip_same=True) and ok

    def write_pose(self, pose: CameraPose, cut: bool = False, suppress_shake: bool = False) -> bool:
        """Send one frame of camera, in the order of the clean-cut recipe (camera-motion.md 7).

        1. FOV (stage target, its per-frame copy, the camera), 2. roll, 3. target + eye
        in one write, 4. optionally the screen-shake offset to 0. On a ``cut`` the
        game's own copy of the lock is asserted first, so the lock holds from the
        very next frame. One tick is enough: nothing has to be repeated.
        """
        paused = self.is_paused()
        if paused is None:
            return False
        freeze = self.game.freeze
        if paused:
            # The pause camera stopped (any freeze method): nobody writes gfCamera, the block goes out
            # whole, FOV and roll included (pause-camera.md 4).
            if freeze.frozen and freeze.hold_photo():
                return self._write_camera_block(pose)
        elif freeze.native and freeze.hold_native():
            return self._write_camera_block(pose)
        if cut:
            self.game.freeze.reassert()
        ok = True
        if pose.fov is not None:
            ok = self.set_fov(pose.fov) and ok
        if pose.roll is not None:
            ok = self.set_roll(pose.roll) and ok
        ok = self.write_target_eye(pose.target, pose.eye, paused) and ok
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
        p = self.game.p
        base = p.CAM_RECORD_BASE
        current = self.game.read_bytes(base, p.CAM_BLOCK_SIZE)
        if current is None:
            return False

        def f32(field: Field) -> float:
            return struct.unpack_from(">f", current, field.addr - base)[0]

        roll = f32(p.CAM_ROT_Z) if pose.roll is None else float(pose.roll)
        fov = f32(p.CAM_FOV) if pose.fov is None else float(pose.fov)
        aspect = f32(p.CAM_ASPECT)
        if valid_aspect(aspect):
            self.game.aspect = aspect  # what the UI shows follows the game (menus 1.65, a match 1.7323)
        else:
            aspect = self.game.aspect or GAME_DEFAULT_ASPECT
        if not (math.isfinite(roll) and math.isfinite(fov) and 0.001 < fov < 3.1):
            return False
        inputs = (tuple(pose.eye), tuple(pose.target), roll, fov, aspect, current)
        if self._block_cache is not None and self._block_cache[0] == inputs:
            block = bytearray(self._block_cache[1])  # same camera on the same block: same bytes
        else:
            block = bytearray(current)
            eye, target = to_brawl(pose.eye), to_brawl(pose.target)
            if not pack_gfcamera_block(block, eye, target, roll, fov, aspect):
                return False
            self._block_cache = (inputs, bytes(block))
        proj_at, proj_size = self._projection_range()
        proj_at -= base
        projection, restoring = self._project(bytes(block[proj_at : proj_at + proj_size]), pose)
        block[proj_at : proj_at + proj_size] = projection
        roll_at = p.CAM_ROT_Z.addr - base
        self.game.ov[p.CAM_ROT_Z.name].adopt(current[roll_at : roll_at + 4], block[roll_at : roll_at + 4])
        ok = True
        if self.game.ov_stage_fov.active or self.game.ov[p.CAM_FOV_COPY.name].active:
            ok = self.clear_fov()  # the stage FOV is the game's again (a later hand back eases to it)
        if not self.game.write_bytes(base, bytes(block), skip_same=True):
            return False
        if restoring:
            self._projection = None
        return ok

    def read_target_eye(self) -> tuple[Vec3, Vec3] | None:
        """In-game camera -> (origin, position) in Blender space."""
        values = self.game.read_field(self.game.p.CAM_IG_BLOCK)
        if values is None:
            return None
        return to_blender(values[0:3]), to_blender(values[3:6])

    def read_sample(self, frame: int | None = None) -> CameraSample | None:
        """The rendered camera (target, eye, roll, effective FOV, world matrix) in one read."""
        if frame is None:
            frame = self.game.current_frame()
            if frame is None:
                return None
        data = self.game.read_bytes(self.game.p.CAM_RECORD_BASE, self.game.p.CAM_RECORD_SIZE)
        return None if data is None else parse_camera_block(frame, data)

    def _stage_fov_address(self) -> int | None:
        stage = self.game.stage_pointer()
        if stage is None:
            return None
        param = self.game.deref(stage + self.game.p.STAGE_CAMERA_PARAM.offset)
        return None if param is None else param + self.game.p.STAGE_PARAM_FOV.offset

    def stage_default_fov(self) -> float | None:
        """The stage's own FOV (radians): the original if EBC overrides it, else the live value."""
        addr = self._stage_fov_address()
        if addr is None:
            return None
        ov = self.game.ov_stage_fov
        data = ov.original if ov.addr == addr and ov.original is not None else self.game.read_bytes(addr, 4)
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
        game, p = self.game, self.game.p
        value = struct.pack(">f", float(vertical_fov_rad))  # the game takes the vertical FOV in radians
        ok = True
        addr = self._stage_fov_address()
        if addr is not None:
            ov = game.ov_stage_fov
            ov.retarget(addr)  # new stage: forget the old one
            if not ov.active:
                default = self.stage_default_fov()  # before our first write
                if default is not None:
                    game.stage_fov = default
            # The game copies the stage FOV into CAM_FOV_COPY every frame: when the copy
            # (read with this tick's snapshot) already holds what we wrote at this same
            # address, the stage FOV holds it too and writing it again changes nothing.
            if not (ov.active and ov.written == value and game.snapshot_holds(p.CAM_FOV_COPY.addr, value)):
                ok = ov.apply(game, value, check=False)
        ok = game.ov[p.CAM_FOV_COPY.name].apply(game, value, check=False, skip_same=True) and ok
        return game.write_bytes(p.CAM_FOV.addr, value, skip_same=True) and ok

    def clear_fov(self) -> bool:
        """Give the FOV back to the stage (the game eases the camera back to it)."""
        ok = self.game.ov_stage_fov.restore(self.game)
        return self.game.restore_field(self.game.p.CAM_FOV_COPY) and ok

    def set_roll(self, radians: float) -> bool:
        field = self.game.p.CAM_ROT_Z
        return self.game.ov[field.name].apply(self.game, field.pack(float(radians)), check=False, skip_same=True)

    def clear_roll(self) -> bool:
        return self.game.restore_field(self.game.p.CAM_ROT_Z)

    def suppress_screen_shake(self) -> bool:
        """Zero the screen-shake view offset (call every tick)."""
        return self.game.write_field(self.game.p.CAM_SHAKE, 0.0, 0.0, 0.0, skip_same=True)

    def set_front_depth(self, value: float) -> bool:
        self.near = float(value)
        return self._apply_depth(self.game.p.CAM_FRONT_DEPTH, self.near)

    def set_back_depth(self, value: float) -> bool:
        if value >= BACK_DEPTH_INFINITE_THRESHOLD:
            value = BACK_DEPTH_INFINITE
        self.far = float(value)
        return self._apply_depth(self.game.p.CAM_BACK_DEPTH, self.far)

    def _apply_depth(self, field: Field, value: float) -> bool:
        """Near / far clip of the perspective camera.

        While the game is orthographic, near/far belong to the projection EBC writes: the value
        goes into the words restored on the way back to perspective (and becomes the override's
        written value), nothing is sent now.
        """
        data = field.pack(value)
        saved = self._projection
        if saved is None:
            return self.game.override(field, data)
        if not self.game.connected:
            return False
        at = field.addr - self.game.p.CAM_FRONT_DEPTH.addr
        self.game.ov[field.name].adopt(saved[0][at : at + 4], data)
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
        p = self.game.p
        start = p.CAM_FRONT_DEPTH.addr
        return start, p.CAM_PROJECTION.addr + 4 - start

    def _project(self, current: bytes, pose: CameraPose) -> tuple[bytes, bool]:
        """The projection words to send (+0xDC..+0x104), and whether they put the game's back.

        ``current`` = the same range as the game has it now. Ortho: the originals are captured
        first (the documented defaults when the game is already orthographic). The bounds of an
        ``OrthoLens`` use the aspect of ``current`` (+0xE4): the one the game draws with now.
        """
        p = self.game.p
        o_bounds = p.CAM_ORTHO_BOUNDS.addr - p.CAM_FRONT_DEPTH.addr
        o_type = p.CAM_PROJECTION.addr - p.CAM_FRONT_DEPTH.addr
        if pose.ortho is None:
            return self._game_words(current)
        out = bytearray(current)
        if self._projection is None:
            if _U32.unpack_from(current, o_type)[0] == PROJECTION_PERSPECTIVE:
                self._projection = (current[0:8], current[o_bounds : o_bounds + 16], current[o_type : o_type + 4])
            else:  # already orthographic (a state saved so): the game's perspective defaults
                near_far = bytearray(struct.pack(">2f", *DEFAULT_NEAR_FAR))
                for field in (p.CAM_FRONT_DEPTH, p.CAM_BACK_DEPTH):
                    ov = self.game.ov[field.name]
                    if ov.written is not None:  # the user's near / far clip
                        at = field.addr - p.CAM_FRONT_DEPTH.addr
                        near_far[at : at + 4] = ov.written
                bounds = struct.pack(">4f", *DEFAULT_ORTHO_BOUNDS)
                self._projection = (bytes(near_far), bounds, _U32.pack(PROJECTION_PERSPECTIVE))
        (aspect,) = struct.unpack_from(">f", current, p.CAM_ASPECT.addr - p.CAM_FRONT_DEPTH.addr)
        if valid_aspect(aspect):
            self.game.aspect = aspect
        else:
            aspect = self.game.aspect or GAME_DEFAULT_ASPECT
        bounds = resolve_ortho(pose.ortho, aspect)
        distance = math.dist(pose.eye, pose.target)
        struct.pack_into(">2f", out, 0, ORTHO_NEAR, distance + ORTHO_FAR_MARGIN)
        struct.pack_into(">4f", out, o_bounds, *bounds)
        _U32.pack_into(out, o_type, PROJECTION_ORTHOGRAPHIC)
        return bytes(out), False

    def _game_words(self, current: bytes) -> tuple[bytes, bool]:
        """``current`` with the game's perspective words put back, and whether there were any kept."""
        saved = self._projection
        if saved is None:
            return bytes(current), False
        p = self.game.p
        o_bounds = p.CAM_ORTHO_BOUNDS.addr - p.CAM_FRONT_DEPTH.addr
        o_type = p.CAM_PROJECTION.addr - p.CAM_FRONT_DEPTH.addr
        out = bytearray(current)
        out[0:8], out[o_bounds : o_bounds + 16], out[o_type : o_type + 4] = saved
        return bytes(out), True

    def _write_projection(self, pose: CameraPose) -> bool:
        """Code Menu lock / pause camera: the projection words in their own write (0x28 bytes).

        Nothing at all (no read) while perspective with nothing of ours in place.
        """
        if pose.ortho is None and self._projection is None:
            return True
        addr, size = self._projection_range()
        current = self.game.read_bytes(addr, size)
        if current is None:
            return False
        data, restoring = self._project(current, pose)
        if not self.game.write_bytes(addr, data, skip_same=not restoring):
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
        if not self.game.connected:
            return False
        addr, size = self._projection_range()
        current = self.game.read_bytes(addr, size, fresh=True)
        if current is None:
            return False
        data, _ = self._game_words(current)
        if not self.game.write_bytes(addr, data):
            return False
        self._projection = None
        return True
