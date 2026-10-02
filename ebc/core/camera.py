"""Camera geometry between Blender and Brawl. Pure math: no ``bpy``, no ``mathutils``.

Matrices are tuples of rows. Blender is Z-up, Brawl Y-up with X mirrored:
``M(x, y, z) = (-x, z, y)``, a proper rotation (det +1) that is its own
inverse. Both cameras look down their local -Z with +Y up, so a camera's
Blender world rotation is ``M @ R`` where ``R`` is its Brawl camera -> world
rotation.

The game camera is a look-at (world-up Brawl Y) followed by a roll around the
view axis (docs/research/camera-motion.md 1.2)::

    view = Rz(roll) . Rx(-pitch) . Ry(-yaw) . T(-eye)
    pitch = asin(d.y), yaw = atan2(-d.x, -d.z), d = normalize(target - eye)

The game resets the Brawl Z of the target to 0 on every frame, even with
Camera Lock, so the target sent must lie on that plane (Blender Y = 0). The
target taken on the camera's view axis, where it crosses the plane, gives any
Blender camera orientation exactly (measured: < 0.0002 px).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]
Rows = Sequence[Sequence[float]]

M_BLENDER_BRAWL: Mat3 = ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0))

# Below this |forward.y| the view axis is parallel to the stage plane.
PARALLEL_EPS = 1e-6
# The target must be farther than this along the view axis (in front of the camera).
MIN_TARGET_DISTANCE = 1.0
# Native freeze, view axis missing the stage plane: target this far along the axis.
FREE_TARGET_DISTANCE = 100.0


# -- small vector / matrix helpers ----------------------------------------------------


def _mul(a: Rows, b: Rows) -> Mat3:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _apply(m: Rows, v: Sequence[float]) -> Vec3:
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


def _transpose(m: Rows) -> Mat3:
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _normalize(v: Sequence[float]) -> Vec3:
    n = math.sqrt(sum(c * c for c in v))
    return (v[0] / n, v[1] / n, v[2] / n)


def rot_x(a: float) -> Mat3:
    c, s = math.cos(a), math.sin(a)
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rot_y(a: float) -> Mat3:
    c, s = math.cos(a), math.sin(a)
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


def rot_z(a: float) -> Mat3:
    c, s = math.cos(a), math.sin(a)
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def to_blender(v: Sequence[float]) -> Vec3:
    return (-float(v[0]), float(v[2]), float(v[1]))


to_brawl = to_blender  # the conversion is its own inverse


def matrix4(rot: Rows, translation: Sequence[float]) -> tuple[tuple[float, float, float, float], ...]:
    """3x3 rotation + translation -> 4x4 rows (what ``mathutils.Matrix`` takes)."""
    return (
        (rot[0][0], rot[0][1], rot[0][2], float(translation[0])),
        (rot[1][0], rot[1][1], rot[1][2], float(translation[1])),
        (rot[2][0], rot[2][1], rot[2][2], float(translation[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


# -- game model -----------------------------------------------------------------------


def look_pitch_yaw(eye: Sequence[float], target: Sequence[float]) -> tuple[float, float]:
    """Brawl eye/target -> gfCamera (pitch, yaw)."""
    d = _normalize([t - e for t, e in zip(target, eye, strict=True)])
    return math.asin(max(-1.0, min(1.0, d[1]))), math.atan2(-d[0], -d[2])


def game_view_matrix(eye: Sequence[float], target: Sequence[float], roll: float) -> tuple[tuple[float, ...], ...]:
    """The view matrix (3x4 rows, Brawl) the game builds from eye, target and roll."""
    pitch, yaw = look_pitch_yaw(eye, target)
    r = _mul(rot_z(roll), _mul(rot_x(-pitch), rot_y(-yaw)))
    t = [-sum(r[i][k] * eye[k] for k in range(3)) for i in range(3)]
    return tuple((*r[i], t[i]) for i in range(3))


def game_camera_rotation(eye: Sequence[float], target: Sequence[float], roll: float) -> Mat3:
    """Brawl camera -> world rotation (inverse of the view rotation)."""
    pitch, yaw = look_pitch_yaw(eye, target)
    return _mul(rot_y(yaw), _mul(rot_x(pitch), rot_z(-roll)))


# -- Brawl -> Blender ---------------------------------------------------------------------


def blender_matrix_from_game(eye: Sequence[float], target: Sequence[float], roll: float):
    """Blender camera world matrix (4x4 rows) showing what the game camera shows.

    ``eye`` and ``target`` are Brawl-space, ``roll`` is the game roll.
    """
    rot = _mul(M_BLENDER_BRAWL, game_camera_rotation(eye, target, roll))
    return matrix4(rot, to_blender(eye))


def blender_matrix_from_world(world: Sequence[float]):
    """Blender camera world matrix from the game's camera -> world matrix (12 floats, 3x4 rows).

    Exact image, screen-shake angles included.
    """
    w = [world[0:4], world[4:8], world[8:12]]
    rot = _mul(M_BLENDER_BRAWL, [row[:3] for row in w])
    return matrix4(rot, to_blender([row[3] for row in w]))


# -- Blender -> Brawl ---------------------------------------------------------------------


def camera_roll(matrix: Rows) -> float:
    """Roll (radians) of a Blender camera from its world matrix (rows, 3x3 or 4x4).

    The camera's right axis is column 0 and its up axis column 1. Without
    roll the right axis is horizontal; rolling by ``a`` around the view axis
    gives ``right.z = k sin a`` and ``up.z = k cos a``. Works whatever drives
    the orientation (Track To, parenting, keyed rotation). The game roll is
    the opposite (measured).
    """
    # columns normalised: a scaled camera object (any scale, even non-uniform) has the same roll
    right_n = math.sqrt(sum(float(matrix[i][0]) ** 2 for i in range(3))) or 1.0
    up_n = math.sqrt(sum(float(matrix[i][1]) ** 2 for i in range(3))) or 1.0
    right_z = float(matrix[2][0]) / right_n
    up_z = float(matrix[2][1]) / up_n
    if abs(right_z) < 1e-9 and abs(up_z) < 1e-9:
        return 0.0  # looking straight up or down: roll undefined
    return math.atan2(right_z, up_z)


def plane_target(matrix: Rows) -> Vec3 | None:
    """Blender-space target of a camera: its view axis (-Z local) where it crosses the stage plane.

    ``matrix`` is the camera's Blender world matrix (4x4 rows). Returns None
    when the axis is parallel to the plane or crosses it behind (or within
    ``MIN_TARGET_DISTANCE`` of) the camera: that orientation cannot be
    shown by the game.
    """
    eye = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
    fwd = _normalize((-float(matrix[0][2]), -float(matrix[1][2]), -float(matrix[2][2])))
    if abs(fwd[1]) <= PARALLEL_EPS:
        return None
    k = -eye[1] / fwd[1]
    if k <= MIN_TARGET_DISTANCE:
        return None
    return (eye[0] + k * fwd[0], 0.0, eye[2] + k * fwd[2])


# -- the whole gfCamera block (native freeze, docs/research/camera-freeze.md 4.2) ----------
#
# With cmAIController+0x94 bit 0x10 cleared, nobody calls gfCamera::update (fn_80018778) any
# more: EBC writes everything it derives. Offsets from gfCamera (0x805B6D20).

GFCAMERA_BLOCK_SIZE = 0x114
_OFF_DIR = 0x78
_OFF_ANGLES = 0xC0  # pitch, yaw, roll, distance, fov, zoom, fov_gx
_OFF_FRUSTUM = 0x104


def _gfcamera_floats(
    eye: Sequence[float], target: Sequence[float], roll: float, fov: float, aspect: float
) -> tuple[list[float], list[float]] | None:
    """(the 55 floats of +0x00..+0xDC, the 4 frustum floats of +0x104), or None if eye == target.

    R = Rz(roll) . Rx(-pitch) . Ry(-yaw) written out (this runs on every tick of a moving camera).
    """
    ex, ey, ez = float(eye[0]), float(eye[1]), float(eye[2])
    tx_, ty_, tz_ = float(target[0]), float(target[1]), float(target[2])
    dx, dy, dz = tx_ - ex, ty_ - ey, tz_ - ez
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if not dist > 1e-6:
        return None
    dx, dy, dz = dx / dist, dy / dist, dz / dist
    pitch = math.asin(max(-1.0, min(1.0, dy)))
    yaw = math.atan2(-dx, -dz)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    m0 = (cy, 0.0, -sy)  # Rx(-pitch) . Ry(-yaw)
    m1 = (sp * sy, cp, sp * cy)
    r0 = (cr * m0[0] - sr * m1[0], cr * m0[1] - sr * m1[1], cr * m0[2] - sr * m1[2])
    r1 = (sr * m0[0] + cr * m1[0], sr * m0[1] + cr * m1[1], sr * m0[2] + cr * m1[2])
    r2 = (cp * sy, -sp, cp * cy)
    t0 = -(r0[0] * ex + r0[1] * ey + r0[2] * ez)
    t1 = -(r1[0] * ex + r1[1] * ey + r1[2] * ez)
    t2 = -(r2[0] * ex + r2[1] * ey + r2[2] * ez)
    tan_y = math.tan(fov / 2.0)
    tan_x = tan_y * aspect
    head = [
        *r0, t0, *r1, t1, *r2, t2,  # +0x00 view
        r0[0], r1[0], r2[0], ex, r0[1], r1[1], r2[1], ey, r0[2], r1[2], r2[2], ez,  # +0x30 world = R^T, eye
        tx_, ty_, tz_, ex, ey, ez,  # +0x60 target, eye
        dx, dy, dz, *r1, *r0,  # +0x78 dir, up, right
        *([0.0] * 9),  # +0x9C..+0xC0 quake translation, 2D offset, angle offsets: no shake
        pitch, yaw, float(roll), dist, float(fov), 1.0, float(fov),  # +0xC0
    ]  # fmt: skip
    return head, [tan_x, tan_y, math.sqrt(1.0 + tan_x * tan_x), math.sqrt(1.0 + tan_y * tan_y)]


def gfcamera_fields(
    eye: Sequence[float], target: Sequence[float], roll: float, fov: float, aspect: float
) -> dict[int, list[float]] | None:
    """What gfCamera::update derives from eye, target (Brawl), roll and FOV (vertical, rad).

    ``{offset: floats}``: view 3x4 +0x00, world 3x4 +0x30, target/eye +0x60, dir/up/right +0x78,
    pitch/yaw/roll/distance/fov/zoom/fov_gx +0xC0, frustum tangents +0x104. Same formula as
    ``freeze_probe.gfcamera_block``, measured within 2.3e-5 of the game's own computation.
    None when eye and target coincide.
    """
    floats = _gfcamera_floats(eye, target, roll, fov, aspect)
    if floats is None:
        return None
    head, frustum = floats
    return {
        0x00: head[0:12],
        0x30: head[12:24],
        0x60: head[24:30],
        _OFF_DIR: head[30:39],
        _OFF_ANGLES: head[48:55],
        _OFF_FRUSTUM: frustum,
    }


_HEAD = struct.Struct(">55f")  # +0x00..+0xDC
_FRUSTUM = struct.Struct(">4f")


def pack_gfcamera_block(
    block: bytearray, eye: Sequence[float], target: Sequence[float], roll: float, fov: float, aspect: float
) -> bool:
    """Put the derived fields into ``block`` (the 0x114 bytes read from gfCamera), shake set to 0.

    Fields not derived (near, far, aspect, viewport, flags) keep the bytes read. False if the
    pose is degenerate (``block`` untouched).
    """
    floats = _gfcamera_floats(eye, target, roll, fov, aspect)
    if floats is None or len(block) < GFCAMERA_BLOCK_SIZE:
        return False
    _HEAD.pack_into(block, 0, *floats[0])
    _FRUSTUM.pack_into(block, _OFF_FRUSTUM, *floats[1])
    return True


@dataclass
class CameraPose:
    """What is sent to the game for one frame. Blender-space points, game roll, vertical FOV (rad).

    ``roll`` / ``fov`` None = not sent (Copy camera roll / Sync FOV off). ``ortho`` = orthographic
    for an ORTHO camera (``ortho_pose``): an ``OrthoLens`` (the bounds are computed with the game's
    aspect when the camera is written) or fixed bounds (top, bottom, left, right); None for a
    perspective one.
    """

    target: Vec3
    eye: Vec3
    roll: float | None = None
    fov: float | None = None
    ortho: "OrthoLens | tuple[float, float, float, float] | None" = None


def axis_target(matrix: Rows, distance: float = FREE_TARGET_DISTANCE) -> Vec3:
    """A point on the camera's view axis, ``distance`` in front of it (Blender space)."""
    fwd = _normalize((-float(matrix[0][2]), -float(matrix[1][2]), -float(matrix[2][2])))
    return (
        float(matrix[0][3]) + distance * fwd[0],
        float(matrix[1][3]) + distance * fwd[1],
        float(matrix[2][3]) + distance * fwd[2],
    )


def pose_from_matrix(
    matrix: Rows,
    fov: float | None = None,
    with_roll: bool = True,
    fallback_target: Sequence[float] | None = None,
    free_target: bool = False,
) -> tuple[CameraPose | None, bool]:
    """Blender camera world matrix -> (pose, representable).

    The target is where the view axis crosses the stage plane. With ``free_target`` (native
    freeze: the game no longer resets the target's Z) an axis that misses the plane gets a
    point ``FREE_TARGET_DISTANCE`` along it instead, so every orientation is representable.
    The plane point is kept when it exists: it is a sensible distance for the game to glide
    from when the camera is handed back. Otherwise, when the orientation cannot be shown,
    the ``fallback_target`` (the last valid one) is used and ``representable`` is False; with
    no fallback the pose is None.
    """
    eye = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
    target = plane_target(matrix)
    if target is None and free_target:
        target = axis_target(matrix)
    ok = target is not None
    if target is None:
        if fallback_target is None:
            return None, False
        target = (float(fallback_target[0]), float(fallback_target[1]), float(fallback_target[2]))
    roll = -camera_roll(matrix) if with_roll else None
    return CameraPose(target, eye, roll, fov), ok


# -- orthographic projection (docs/research/ortho-projection.md) ---------------------------
#
# gfCamera+0x100 (u32) = 1 perspective, 0 orthographic: gfCamera::setGX then calls
# C_MTXOrtho(top +0xE8, bottom +0xEC, left +0xF0, right +0xF4, near +0xDC, far +0xE0) and the g3d
# camera copy SetOrtho. Pure data, never rewritten in a match. In ortho the eye distance does not
# change the picture, only what the near plane cuts: the eye is pulled back ORTHO_EYE_DISTANCE
# along the view axis from the target, near = 1, far = distance + ORTHO_FAR_MARGIN.

PROJECTION_PERSPECTIVE = 1
PROJECTION_ORTHOGRAPHIC = 0
ORTHO_EYE_DISTANCE = 1000.0
ORTHO_NEAR = 1.0
ORTHO_FAR_MARGIN = 2000.0

Bounds = tuple[float, float, float, float]  # top, bottom, left, right (gfCamera +0xE8..+0xF4)


def ortho_fits_height(sensor_fit: str, aspect: float) -> bool:
    """True when Blender's ``ortho_scale`` is the visible HEIGHT (VERTICAL, or AUTO on a tall image)."""
    return sensor_fit == "VERTICAL" or (sensor_fit == "AUTO" and aspect < 1.0)


def ortho_bounds(
    ortho_scale: float, aspect: float, sensor_fit: str = "AUTO", shift_x: float = 0.0, shift_y: float = 0.0
) -> Bounds:
    """Blender ORTHO camera -> gfCamera bounds (top, bottom, left, right), Brawl units.

    ``ortho_scale`` (S) is the visible extent along the fitted axis: the width for AUTO (wide
    image) and HORIZONTAL, the height for VERTICAL. ``aspect`` = width / height of the game
    picture (gfCamera+0xE4). Shifts are in units of S for the three fits (measured <= 0.0004 px).
    """
    s = float(ortho_scale)
    if ortho_fits_height(sensor_fit, aspect):
        half_h = s / 2.0
        half_w = half_h * aspect
    else:
        half_w = s / 2.0
        half_h = half_w / aspect
    dx, dy = float(shift_x) * s, float(shift_y) * s
    return (half_h + dy, -half_h + dy, -half_w + dx, half_w + dx)


@dataclass(frozen=True)
class OrthoLens:
    """A Blender ORTHO camera's lens. Its bounds depend on the picture aspect, so they are only
    computed when the camera is written, with the aspect the game has then (gfCamera+0xE4: 1.65 in
    the menus, 1.7323 in a match; a value cached at connect goes stale after a savestate)."""

    scale: float
    sensor_fit: str = "AUTO"
    shift_x: float = 0.0
    shift_y: float = 0.0

    def bounds(self, aspect: float) -> Bounds:
        return ortho_bounds(self.scale, aspect, self.sensor_fit, self.shift_x, self.shift_y)


def resolve_ortho(ortho, aspect: float) -> Bounds | None:
    """Bounds of a pose's ``ortho`` (lens or fixed bounds) for the game's current ``aspect``."""
    if ortho is None:
        return None
    if isinstance(ortho, OrthoLens):
        return ortho.bounds(aspect)
    return (float(ortho[0]), float(ortho[1]), float(ortho[2]), float(ortho[3]))


def ortho_from_bounds(bounds: Sequence[float], aspect: float, sensor_fit: str = "AUTO") -> tuple[float, float, float]:
    """gfCamera bounds -> Blender (ortho_scale, shift_x, shift_y). Inverse of ``ortho_bounds``.

    The scale comes from the fitted axis (the other one follows from the render aspect).
    """
    t, b, left, r = (float(v) for v in bounds)
    s = (t - b) if ortho_fits_height(sensor_fit, aspect) else (r - left)
    return s, (r + left) / 2.0 / s, (t + b) / 2.0 / s


def valid_bounds(bounds: Sequence[float]) -> bool:
    return (
        len(bounds) == 4
        and all(math.isfinite(v) and abs(v) < 1e7 for v in bounds)
        and bounds[0] > bounds[1]
        and bounds[3] > bounds[2]
    )


def ortho_pose(
    pose: CameraPose, matrix: Rows, ortho: "OrthoLens | Bounds", distance: float = ORTHO_EYE_DISTANCE
) -> CameraPose:
    """The pose sent for an ORTHO camera: same target and roll, eye pulled back on the view axis.

    ``ortho``: the camera's ``OrthoLens`` (bounds computed at write time with the game's aspect)
    or fixed bounds. No FOV is sent (the projection does not use it; the frustum tangents keep
    the current one).
    """
    fwd = _normalize((-float(matrix[0][2]), -float(matrix[1][2]), -float(matrix[2][2])))
    t = pose.target
    eye = (t[0] - distance * fwd[0], t[1] - distance * fwd[1], t[2] - distance * fwd[2])
    if not isinstance(ortho, OrthoLens):
        ortho = resolve_ortho(ortho, 1.0)  # type: ignore[assignment]
    return CameraPose(pose.target, eye, pose.roll, None, ortho)


def ortho_scale_for_framing(distance: float, vertical_fov: float, aspect: float, sensor_fit: str = "AUTO") -> float:
    """ortho_scale showing what a perspective camera shows at ``distance`` (its target plane)."""
    half_h = float(distance) * math.tan(float(vertical_fov) / 2.0)
    return 2.0 * half_h if ortho_fits_height(sensor_fit, aspect) else 2.0 * half_h * aspect


# Axonometric presets (docs/research/ortho-projection.md 4.2): elevation above the horizontal.
# Blender rotation_euler (XYZ) = (90 deg - elevation, 0, azimuth).
ELEVATION_ISOMETRIC = math.degrees(math.atan(1.0 / math.sqrt(2.0)))  # 35.264: true isometric
ELEVATION_DIMETRIC = 30.0  # 2:1 "video game" dimetric: floor edges at 26.565 deg on screen
AXONOMETRIC_AZIMUTHS = (45.0, 135.0, 225.0, 315.0)


def axonometric_rotation(elevation_deg: float, azimuth_deg: float) -> Mat3:
    """Blender world rotation of a camera with rotation_euler (90 - elevation, 0, azimuth), XYZ."""
    return _mul(rot_z(math.radians(azimuth_deg)), rot_x(math.radians(90.0 - elevation_deg)))


def axonometric_matrix(target: Sequence[float], elevation_deg: float, azimuth_deg: float, distance: float):
    """Blender camera world matrix (4x4 rows) of an axonometric view centred on ``target``.

    No roll (the right axis is horizontal); the camera sits ``distance`` back along its view axis.
    """
    rot = axonometric_rotation(elevation_deg, azimuth_deg)
    fwd = (-rot[0][2], -rot[1][2], -rot[2][2])
    loc = tuple(float(target[i]) - distance * fwd[i] for i in range(3))
    return matrix4(rot, loc)


# -- one gfCamera read (Record, Brawl -> Blender) -----------------------------------------


@dataclass
class CameraSample:
    """The rendered game camera of one frame, raw Brawl values."""

    frame: int
    target: Vec3
    eye: Vec3
    roll: float
    fov: float  # effective vertical FOV (rad), after easing and zoom
    world: tuple[float, ...]  # camera -> world 3x4 rows (screen-shake angles included)
    ortho: Bounds | None = None  # orthographic bounds when gfCamera+0x100 == 0, else None
    aspect: float | None = None  # gfCamera+0xE4 of the same read (the bounds' aspect), None if unread

    def blender_matrix(self, include_shake: bool = False):
        if include_shake:
            return blender_matrix_from_world(self.world)
        return blender_matrix_from_game(self.eye, self.target, self.roll)


# Offsets in the CAM_RECORD block (gfCamera, from 0x805B6D20).
_OFF_WORLD = 0x30
_OFF_TARGET = 0x60
_OFF_EYE = 0x6C
_OFF_ROLL = 0xC8
_OFF_FOV_GX = 0xD8
_OFF_ASPECT = 0xE4
_OFF_BOUNDS = 0xE8
_OFF_PROJECTION = 0x100


def parse_camera_block(frame: int, data: bytes) -> CameraSample | None:
    """gfCamera bytes (0x104 from 0x805B6D20) -> sample, None if any value is not finite.

    A block that reaches +0x104 also gives the projection: ``ortho`` holds the bounds when
    +0x100 is 0 and they make a box (anything else is read as perspective).
    """
    if len(data) < _OFF_FOV_GX + 4:
        return None
    world = struct.unpack_from(">12f", data, _OFF_WORLD)
    target = struct.unpack_from(">3f", data, _OFF_TARGET)
    eye = struct.unpack_from(">3f", data, _OFF_EYE)
    (roll,) = struct.unpack_from(">f", data, _OFF_ROLL)
    (fov,) = struct.unpack_from(">f", data, _OFF_FOV_GX)
    values = (*world, *target, *eye, roll, fov)
    if not all(math.isfinite(v) and abs(v) < 1e7 for v in values) or not 0.001 < fov < 3.1:
        return None
    if math.dist(eye, target) < 1e-6:
        return None
    ortho = aspect = None
    if len(data) >= _OFF_ASPECT + 4:
        (value,) = struct.unpack_from(">f", data, _OFF_ASPECT)
        aspect = value if valid_aspect(value) else None
    if len(data) >= _OFF_PROJECTION + 4 and struct.unpack_from(">I", data, _OFF_PROJECTION)[0] == 0:
        bounds = struct.unpack_from(">4f", data, _OFF_BOUNDS)
        if valid_bounds(bounds):
            ortho = bounds
    return CameraSample(frame, target, eye, roll, fov, world, ortho, aspect)


def valid_aspect(value: float) -> bool:
    return math.isfinite(value) and 0.5 < value < 4.0
