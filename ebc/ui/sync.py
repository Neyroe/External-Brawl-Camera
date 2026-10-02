"""Synchronisation loop: a ``bpy.app.timers`` callback at ~60 Hz.

Started and stopped explicitly (buttons / ``ebc.sync_start`` /
``ebc.sync_stop``); no global key capture. ``tick()`` can also be called
directly (the headless smoke test does, since timers do not run in
background mode).

The camera is computed and offered to the game on every tick; the memory phase
(``Game.tick``) sends only what differs from what the game holds now, read in
a few merged calls (docs/DEVELOPMENT.md "Memory access budget"). On the
Blender side only what changed is touched (object locations, visibility, the
frame counter property), to avoid needless depsgraph updates and redraws.

Latency (docs/research/camera-motion.md 1.1): a write made after seeing the
frame counter at N is taken by frame N+1. In Follow game frame mode the
Blender timeline is therefore set to N + LOOKAHEAD, so the pose sent is the
one of the frame the game renders next.

What is sent (Blender -> Brawl): the camera's world matrix only. The target is
where its view axis crosses the stage plane (``core.camera.plane_target``):
no Track To and no Origin are needed. The Origin object is a visual aid. With
the native freeze (default) the game no longer resets the target's Z, so an
axis that misses the plane is sent too (a point along it).

Projection: an ORTHO camera makes the game orthographic (ortho_scale, sensor fit and
shifts -> the game's bounds, docs/research/ortho-projection.md), in the same write as the
camera; a PERSP camera, or a tick that sends nothing (Brawl -> Blender, Hand back, Record),
puts the game's perspective back. An EBC_Blend between an ORTHO and a PERSP camera
switches projection at mid-transition (influence 0.5).
"""

import math
import time
import traceback

import bpy
from mathutils import Matrix

from .. import runtime
from ..core import camera as cam_math
from ..core import game as g
from . import props, record

INTERVAL = 1.0 / 60.0
DEVSERVER_REFRESH_TICKS = 120

LOOKAHEAD = 1  # frames: see the module docstring

_state = {
    "ticks": 0,
    "last_frame": None,
    "error": None,
    "last_camera": None,  # pointer of the camera sent on the previous tick (a change = a cut)
    "cut_requested": False,
    "cuts": 0,
    "last_target": None,  # last representable target (Blender space)
    "camera_warning": None,
    "handed_back": False,
    "io": None,  # IOStats of the last tick (backend calls, bytes, skipped writes, time in calls)
    "tick_ms": 0.0,  # whole tick, smoothed
    "io_ms": 0.0,  # time inside backend calls, smoothed
}
SMOOTH = 0.1  # exponential smoothing of the times shown in the Connection panel

UNREPRESENTABLE = "Camera does not look at the stage plane: the game keeps the last valid aim"

ORIGIN_CONSTRAINTS = {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}
EPSILON = 1e-4

# EBC_Blend transition camera (operators.build_transition): Copy Transforms A then B.
BLEND_NAME = "EBC_Blend"
CON_FROM = "EBC from"
CON_TO = "EBC to"
# Influence of B at which a transition between an ORTHO and a PERSP camera switches projection.
PROJECTION_SWITCH = 0.5


def _differs(a, b) -> bool:
    return any(abs(x - y) > EPSILON for x, y in zip(a, b, strict=True))


def is_running() -> bool:
    return bpy.app.timers.is_registered(_timer_callback)


def last_error() -> str | None:
    return _state["error"]


def camera_warning() -> str | None:
    """Warning about the camera being sent (shown in the Camera panel)."""
    return _state["camera_warning"]


def io_summary() -> str | None:
    """'12 calls · 0.04 ms I/O · tick 0.31 ms' for the Connection panel (None before a tick)."""
    io = _state["io"]
    if io is None:
        return None
    return f"{io.calls} calls · {_state['io_ms']:.2f} ms I/O · tick {_state['tick_ms']:.2f} ms"


def request_cut() -> None:
    """Send the next tick as a cut (clean-cut recipe), even if the camera did not change."""
    _state["cut_requested"] = True


def start() -> bool:
    if not runtime.get_game().connected:
        _state["error"] = "Not connected to Dolphin"
        return False
    _state.update(
        ticks=0,
        last_frame=None,
        error=None,
        last_camera=None,
        camera_warning=None,
        handed_back=False,
        cuts=0,
        io=None,
        tick_ms=0.0,
        io_ms=0.0,
    )
    if not is_running():
        bpy.app.timers.register(_timer_callback, first_interval=0.0, persistent=False)
    return True


def stop() -> None:
    if is_running():
        bpy.app.timers.unregister(_timer_callback)
    game = runtime.get_game()
    if game.ortho_active and game.connected:
        game.restore_projection()  # the game's perspective back: nothing drives the ortho camera now
    _tag_redraw()


def _timer_callback():
    try:
        keep_going = tick(bpy.context.scene)
    except Exception as exc:
        traceback.print_exc()
        _state["error"] = f"Sync stopped: {exc}"
        keep_going = False
    if not keep_going:
        _tag_redraw()
        return None
    return INTERVAL


def _tag_redraw() -> None:
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# -- rig resolution ---------------------------------------------------------------


def effective_camera(s, scene):
    """The scene camera when it is one of the EBC cameras (timeline markers
    switch it), otherwise the camera picked in the Rig panel."""
    listed = {item.camera for item in s.cameras if item.camera is not None}
    if scene is not None and scene.camera is not None and scene.camera in listed:
        return scene.camera
    return s.camera


def effective_origin(s, camera):
    """The object shown as the aim point: the camera's tracking target, else the Rig origin.

    Not needed to aim any more (the target comes from the camera's view axis);
    Brawl -> Blender moves it onto the game's target, Keep origin on the stage
    plane pins it to Y = 0.
    """
    if camera is not None:
        for con in camera.constraints:
            if con.type in ORIGIN_CONSTRAINTS and con.enabled and getattr(con, "target", None) is not None:
                return con.target
    return s.origin


def camera_vertical_fov(camera, scene) -> float:
    cam = camera.data
    r = scene.render
    return g.vertical_fov(
        cam.lens,
        cam.sensor_width,
        cam.sensor_height,
        cam.sensor_fit,
        r.resolution_x * r.pixel_aspect_x,
        r.resolution_y * r.pixel_aspect_y,
    )


# -- the tick ------------------------------------------------------------------------


def tick(scene) -> bool:
    """One synchronisation step. Returns False when the loop must stop.

    Order (docs/DEVELOPMENT.md "Memory access budget"): poll (3 reads), then the Blender side
    that the pose depends on (timeline, camera matrix), then the memory phase inside
    ``game.tick()`` (snapshot read in a few merged calls, writes sent only when they change
    something), then the Blender objects updated from what was read. The snapshot is taken
    right before the writes, so it is as fresh as the old one-read-per-value code.
    """
    game = runtime.get_game()
    if scene is None or not game.connected:
        _state["error"] = "Disconnected from Dolphin"
        return False
    t0 = time.perf_counter()
    io0 = game.io.copy()
    s = props.settings_of(scene)
    _state["ticks"] += 1
    ticks = _state["ticks"]

    result = game.poll()  # the only frame-counter read of the tick
    if result.frame is None:
        _state["error"] = game.last_error or "Could not read the game frame"
        return False
    frame = result.frame
    if s.frame_number != frame:
        s.frame_number = frame
    if result.stage_id is not None and s.stage_id != (result.stage_id & 0x7FFFFFFF):
        s.stage_id = result.stage_id & 0x7FFFFFFF

    _sync_timeline(s, scene, frame)

    camera = effective_camera(s, scene)
    origin = effective_origin(s, camera)
    recording = record.is_recording()
    sending = camera is not None and s.camera_direction == "BLENDER_TO_GAME" and not recording
    if sending and _hand_back(s, game):
        sending = False
    receiving = camera is not None and s.camera_direction == "GAME_TO_BLENDER" and not recording
    pose = None
    cut = False
    if sending:
        pointer = camera.as_pointer()
        cut = _state["cut_requested"] or pointer != _state["last_camera"]
        _state["cut_requested"] = False
        _state["last_camera"] = pointer
        if cut:
            _state["cuts"] += 1
        pose = camera_pose(s, scene, camera, origin, free_target=game.native_freeze)
    else:
        _state["last_camera"] = None

    sample = None
    positions = None
    with game.tick():
        # Fighters are resolved once per tick and shared by positions and tints
        # (Zelda/Sheik switch instance, so nothing is cached across ticks).
        refs = game.fighters() if s.show_player_positions or game.needs_fighter_refresh() else {}
        if s.show_player_positions:
            positions = {slot: game.player_position(slot, refs) for slot in props.SLOTS}
        game.refresh_fighters(refs)
        game.assert_display()
        game.hold_green_screen()  # the game drops the fill at the end of a match, a savestate too
        if sending:
            game.hold_freeze()
        else:
            game.restore_projection()  # no I/O unless EBC made the game orthographic
        if s.disable_screen_shake and not sending:
            game.suppress_screen_shake()  # when sending, it goes last in the camera writes
        if pose is not None:
            game.write_camera_pose(pose, cut=cut, suppress_shake=bool(getattr(s, "disable_screen_shake", False)))
        elif receiving:
            sample = game.read_camera_sample(frame)

    if positions is not None:
        _sync_players(s, positions)
    if sample is not None:
        apply_game_camera(sample, camera, origin, scene)

    if ticks % DEVSERVER_REFRESH_TICKS == 0 and runtime.devserver.available:
        runtime.devserver.status()

    _state["last_frame"] = frame
    io = game.io.since(io0)
    _state["io"] = io
    k = 1.0 if ticks == 1 else SMOOTH
    _state["tick_ms"] += k * ((time.perf_counter() - t0) * 1000.0 - _state["tick_ms"])
    _state["io_ms"] += k * (io.ns / 1e6 - _state["io_ms"])
    return True


def _sync_timeline(s, scene, frame: int) -> None:
    if s.timeline_mode == "FOLLOW":
        want = frame + LOOKAHEAD
        if scene.frame_current != want:
            scene.frame_set(want)
    elif s.timeline_mode == "START_AT":
        last = _state["last_frame"]
        if last is not None and last < s.start_frame <= frame:
            play_animation()


def play_animation() -> bool:
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return False
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        if screen.is_animation_playing:
            return True
        try:
            with bpy.context.temp_override(window=window, screen=screen):
                bpy.ops.screen.animation_play()
            return True
        except Exception:
            traceback.print_exc()
            return False
    return False


def _sync_players(s, positions) -> None:
    """Move PLAYER_1..4 to the positions read in the memory phase (None = not in the match)."""
    for slot in props.SLOTS:
        obj = s.player(slot)
        if obj is None:
            continue
        pos = positions.get(slot)
        visible = pos is not None
        if visible and _differs(obj.location, pos):
            obj.location = pos
        try:
            if obj.hide_get() == visible:
                obj.hide_set(not visible)
        except RuntimeError:
            pass  # object not in the active view layer


def _keep_origin_on_plane(origin) -> None:
    if origin.location.y != 0.0:
        origin.location.y = 0.0
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        rv3d = area.spaces[0].region_3d
        if rv3d is not None and rv3d.view_perspective == "CAMERA":
            rv3d.view_location.y = origin.matrix_world.translation.y


def _hand_back(s, game: g.Game) -> bool:
    """Hand back to the game: True while the (animatable) option is on.

    The lock is released once when it turns on (the game then eases from our
    camera to its own, camera-motion.md 3) and taken again when it turns off.
    """
    if s.hand_back_to_game:
        if not _state["handed_back"]:
            game.freeze_camera(False)
            _state["handed_back"] = True
        return True
    if _state["handed_back"]:
        _state["handed_back"] = False
        game.freeze_camera(s.lock_game_camera)
        _state["cut_requested"] = True
    return False


def blend_ends(camera):
    """(A, B, influence of B) of an EBC_Blend camera, None for any other camera."""
    if camera is None:
        return None
    a = camera.constraints.get(CON_FROM)
    b = camera.constraints.get(CON_TO)
    if a is None or b is None or a.target is None or b.target is None:
        return None
    return a.target, b.target, b.influence


def is_ortho(camera) -> bool:
    return camera is not None and camera.type == "CAMERA" and camera.data.type == "ORTHO"


def projection_camera(camera):
    """The camera whose projection is sent for ``camera``: itself, except for an EBC_Blend
    between an ORTHO and a PERSP camera: A before mid-transition, B from there on."""
    ends = blend_ends(camera)
    if ends is None:
        return camera
    a, b, t = ends
    if is_ortho(a) == is_ortho(b):
        return camera
    return b if t >= PROJECTION_SWITCH else a


def game_aspect() -> float:
    """The game's picture aspect as last read (no I/O)."""
    return runtime.get_game().aspect or g.GAME_DEFAULT_ASPECT


def camera_pose(s, scene, camera, origin=None, free_target: bool = False):
    """The pose to send for ``camera`` (Blender side only, no memory access); None if none.

    ``free_target`` (native freeze): the target may leave the stage plane, so an axis that
    misses it is still sent (a point along it) instead of keeping the last target.
    An ORTHO camera gives an orthographic pose: same target and roll, bounds from its
    ortho_scale / sensor fit / shifts and the game aspect, eye pulled back along the axis.
    """
    if getattr(s, "keep_origin_on_plane", False) and origin is not None:
        _keep_origin_on_plane(origin)
    proj = projection_camera(camera)
    ortho = is_ortho(proj)
    fov = None
    if s.sync_fov and camera.type == "CAMERA" and not ortho:
        fov = camera_vertical_fov(camera if proj is camera else proj, scene)
    mw = [tuple(row) for row in camera.matrix_world]
    # Measured: a positive game roll turns the picture counter-clockwise, a positive
    # Blender roll (right side up) clockwise: the pose carries the opposite roll.
    pose, ok = cam_math.pose_from_matrix(mw, fov, bool(s.copy_camera_roll), _state["last_target"], free_target)
    _state["camera_warning"] = None if ok else UNREPRESENTABLE
    if pose is not None and ok:
        _state["last_target"] = pose.target
    if pose is not None and ortho:
        d = proj.data
        # the bounds follow from the game's aspect when the block is written (Game._project)
        lens = cam_math.OrthoLens(d.ortho_scale, d.sensor_fit, d.shift_x, d.shift_y)
        pose = cam_math.ortho_pose(pose, mw, lens)
    return pose


def _blender_to_game(s, scene, game: g.Game, camera, origin=None, cut: bool = False) -> bool:
    pose = camera_pose(s, scene, camera, origin, free_target=game.native_freeze)
    if pose is None:
        return False
    return game.write_camera_pose(pose, cut=cut, suppress_shake=bool(getattr(s, "disable_screen_shake", False)))


def game_camera_matrix(sample, include_shake: bool = False) -> Matrix:
    return Matrix(sample.blender_matrix(include_shake))


def lens_for(camera, scene, fov: float) -> float:
    cam = camera.data
    r = scene.render
    return g.lens_for_vertical_fov(
        fov,
        cam.sensor_width,
        cam.sensor_height,
        cam.sensor_fit,
        r.resolution_x * r.pixel_aspect_x,
        r.resolution_y * r.pixel_aspect_y,
    )


def _game_to_blender(game: g.Game, camera, origin, scene=None) -> bool:
    """Copy the rendered game camera: position, orientation (roll included) and FOV.

    A tracking constraint on the camera overrides the copied orientation;
    the Origin is moved onto the game's target so it still aims right.
    """
    sample = game.read_camera_sample()
    if sample is None:
        return False
    apply_game_camera(sample, camera, origin, scene)
    return True


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


def apply_game_camera(sample, camera, origin, scene=None) -> None:
    """Blender side of Brawl -> Blender: camera matrix, Origin on the target, lens from the FOV
    (or ORTHO and ortho_scale when the game is orthographic)."""
    mw = game_camera_matrix(sample)
    if not same_pose(camera, mw):
        set_pose_keep_scale(camera, mw)
    target = cam_math.to_blender(sample.target)
    if origin is not None and origin != camera and _differs(origin.matrix_world.translation, target):
        origin.matrix_world.translation = target
    apply_projection(camera, sample)
    if scene is not None and camera.type == "CAMERA" and camera.data.type == "PERSP":
        lens = lens_for(camera, scene, sample.fov)
        if math.isfinite(lens) and abs(camera.data.lens - lens) > EPSILON:
            camera.data.lens = lens
