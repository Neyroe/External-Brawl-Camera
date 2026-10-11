"""Blender cameras as EBC uses them: which one is synced, its optics against the render size, the
rig objects. No state and no import of the other UI modules (props, sync, stages, operators all
import this one).
"""

import math

import bpy
from mathutils import Matrix

from .. import runtime
from ..core import camera as cam_math

RIG_COLLECTION = "EBC"
# Default framing: in front of the stage. Brawl +Z (towards the viewer) is Blender +Y.
RIG_CAMERA_LOCATION = (0.0, 150.0, 20.0)
ORIGIN_CONSTRAINTS = {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}
EPSILON = 1e-4


# -- which camera ---------------------------------------------------------------------------------


def effective_camera(s, scene):
    """The scene camera when it is one of the EBC cameras (timeline markers
    switch it), otherwise the camera picked in the Rig panel."""
    listed = {item.camera for item in s.cameras if item.camera is not None}
    if scene is not None and scene.camera is not None and scene.camera in listed:
        return scene.camera
    return s.camera


def synced_camera(s):
    """The camera the sync sends (scene camera if listed, else the Rig camera), or None."""
    cam = effective_camera(s, s.id_data)
    return cam if cam is not None and cam.type == "CAMERA" else None


def tracking_target(camera):
    """The target of the camera's first enabled tracking constraint (Track To...), else None."""
    for con in camera.constraints:
        if con.type in ORIGIN_CONSTRAINTS and con.enabled and getattr(con, "target", None) is not None:
            return con.target
    return None


def effective_origin(s, camera):
    """The object shown as the aim point: the camera's tracking target, else the Rig origin.

    Not needed to aim any more (the target comes from the camera's view axis);
    Brawl -> Blender moves it onto the game's target, Keep origin on the stage
    plane pins it to Y = 0.
    """
    target = tracking_target(camera) if camera is not None else None
    return target if target is not None else s.origin


def aimed_point(camera):
    """What the camera looks at: its tracking target, else its view axis on the stage plane
    (else 100 units ahead)."""
    target = tracking_target(camera)
    if target is not None:
        return tuple(target.matrix_world.translation)
    mw = [tuple(row) for row in camera.matrix_world]
    return cam_math.plane_target(mw) or cam_math.axis_target(mw)


def is_ortho(camera) -> bool:
    return camera is not None and camera.type == "CAMERA" and camera.data.type == "ORTHO"


def ensure_listed(s, cam) -> None:
    """Add ``cam`` to the Cameras list when it is not there yet."""
    if all(item.camera != cam for item in s.cameras):
        s.cameras.add().camera = cam


# -- optics against the render size ---------------------------------------------------------------


def render_size(scene) -> tuple[float, float]:
    """The render picture's width and height, pixel aspect included."""
    r = scene.render
    return r.resolution_x * r.pixel_aspect_x, r.resolution_y * r.pixel_aspect_y


def render_aspect(scene) -> float:
    width, height = render_size(scene)
    return width / max(1e-6, height)


def game_aspect() -> float:
    """The game's picture aspect as last read (no I/O)."""
    return runtime.get_game().aspect or cam_math.GAME_DEFAULT_ASPECT


def camera_vertical_fov(camera, scene) -> float:
    cam = camera.data
    return cam_math.vertical_fov(cam.lens, cam.sensor_width, cam.sensor_height, cam.sensor_fit, *render_size(scene))


def lens_for(camera, scene, fov: float) -> float:
    """Focal length giving ``camera`` the vertical ``fov`` (radians) at the render size."""
    cam = camera.data
    return cam_math.lens_for_vertical_fov(fov, cam.sensor_width, cam.sensor_height, cam.sensor_fit, *render_size(scene))


def default_fov() -> float:
    """The stage's own vertical FOV (radians) as last read from the game, else 30 degrees."""
    fov = runtime.get_game().stage_fov
    return fov if fov is not None else math.radians(cam_math.GAME_DEFAULT_FOV_DEG)


def set_default_fov(camera, scene, fov: float | None = None) -> None:
    if camera.data.type != "PERSP":
        return
    camera.data.lens = lens_for(camera, scene, default_fov() if fov is None else fov)


def keep_framing_ortho(cam, scene, distance: float | None = None) -> None:
    """PERSP -> ORTHO keeping roughly the framing: the ortho_scale showing what the perspective
    camera shows at ``distance`` (default: its view axis on the stage plane, else 100 units)."""
    if distance is None:
        mw = [tuple(row) for row in cam.matrix_world]
        target = cam_math.plane_target(mw) or cam_math.axis_target(mw)
        eye = cam.matrix_world.translation
        distance = max(1.0, sum((eye[i] - target[i]) ** 2 for i in range(3)) ** 0.5)
    fov = camera_vertical_fov(cam, scene)
    cam.data.ortho_scale = cam_math.ortho_scale_for_framing(distance, fov, render_aspect(scene), cam.data.sensor_fit)


# -- pose -----------------------------------------------------------------------------------------


def set_pose_keep_scale(obj, mw: Matrix) -> None:
    """Give ``obj`` the location and rotation of ``mw`` (scale 1), keeping its own scale.

    The scale of a camera object only changes how it is drawn in the viewport: copying the
    game camera onto it must not reset it. The local scale is put back (``matrix_world`` may
    not be evaluated yet right after the scale was changed).
    """
    scale = obj.scale.copy()
    obj.matrix_world = mw
    obj.scale = scale


def same_pose(obj, mw: Matrix) -> bool:
    """True when ``obj`` already has the location and rotation of ``mw`` (its scale ignored)."""
    current = obj.matrix_world.normalized()
    return not any(
        abs(a - b) > EPSILON for ra, rb in zip(current, mw, strict=True) for a, b in zip(ra, rb, strict=True)
    )


def game_camera_matrix(sample, include_shake: bool = False) -> Matrix:
    return Matrix(sample.blender_matrix(include_shake))


def apply_projection(camera, sample) -> None:
    """The game's projection on the Blender camera: ORTHO with the ortho_scale (and shifts) of
    the game's bounds when the game is orthographic, PERSP back when it is not."""
    if camera.type != "CAMERA":
        return
    data = camera.data
    if sample.ortho is None:
        if data.type == "ORTHO":
            data.type = "PERSP"
        return
    if data.type != "ORTHO":
        data.type = "ORTHO"
    aspect = sample.aspect or game_aspect()  # the aspect read with the bounds
    scale, shift_x, shift_y = cam_math.ortho_from_bounds(sample.ortho, aspect, data.sensor_fit)
    for attr, value in (("ortho_scale", scale), ("shift_x", shift_x), ("shift_y", shift_y)):
        if math.isfinite(value) and abs(getattr(data, attr) - value) > EPSILON:
            setattr(data, attr, value)


# -- rig objects ----------------------------------------------------------------------------------


def rig_collection(context):
    coll = bpy.data.collections.get(RIG_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(RIG_COLLECTION)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def new_empty(name, coll, display="PLAIN_AXES", size=10.0):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = display
    obj.empty_display_size = size
    coll.objects.link(obj)
    return obj


def track(camera, target) -> None:
    """Aim ``camera`` at ``target``: its first tracking constraint, else a new Track To."""
    for con in camera.constraints:
        if con.type in ORIGIN_CONSTRAINTS:
            con.target = target
            return
    con = camera.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"


def new_camera(context, name, origin):
    """A camera in the EBC collection, at the default framing and FOV, tracking ``origin`` if given."""
    coll = rig_collection(context)
    data = bpy.data.cameras.new(name)
    data.clip_start = 1.0
    data.clip_end = 5000.0
    cam = bpy.data.objects.new(name, data)
    cam.location = RIG_CAMERA_LOCATION
    coll.objects.link(cam)
    set_default_fov(cam, context.scene)
    if origin is not None:
        track(cam, origin)
    return cam


def bind_marker(scene, frame: int, cam):
    """The timeline marker at ``frame`` (created if none) switches to ``cam``."""
    marker = next((m for m in scene.timeline_markers if m.frame == frame), None)
    if marker is None:
        marker = scene.timeline_markers.new(cam.name, frame=frame)
    marker.camera = cam
    return marker
