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
from dataclasses import dataclass, field
from typing import NamedTuple

import bpy

from .. import runtime
from ..core import camera as cam_math
from ..core import emucontrol
from ..core import freeze as fz
from ..core import game as g
from ..core import stages as st
from ..core.memory import IOStats
from ..core.pause_at import PauseAtFrame
from . import cameras, props, record, stage_presets, util
from .cameras import effective_camera, effective_origin, is_ortho

INTERVAL = 1.0 / 60.0
DEVSERVER_REFRESH_TICKS = 120

LOOKAHEAD = 1  # frames: see the module docstring


@dataclass
class SyncState:
    """What the sync loop keeps between ticks (one per process: ``_state``)."""

    ticks: int = 0
    last_frame: int | None = None
    error: str | None = None
    last_camera: int | None = None  # pointer of the camera sent on the previous tick (a change = a cut)
    cut_requested: bool = False
    cuts: int = 0
    last_target: tuple | None = None  # last representable target (Blender space)
    camera_warning: str | None = None
    handed_back: bool = False
    io: IOStats | None = None  # backend calls, bytes, skipped writes, time in calls of the last tick
    still_ticks: int = 0  # ticks in a match with the frame counter still (Play / Pause state)
    tick_ms: float = 0.0  # whole tick, smoothed
    io_ms: float = 0.0  # time inside backend calls, smoothed
    pause_at: PauseAtFrame = field(default_factory=PauseAtFrame)  # Pause at frame: the last frame seen
    pause_message: str | None = None  # what the last Pause at frame did (Timeline panel)

    def restart(self) -> None:
        """A new sync run: everything but the pending cut and the last valid target."""
        fresh = SyncState(cut_requested=self.cut_requested, last_target=self.last_target)
        self.__dict__.update(fresh.__dict__)


_state = SyncState()
SMOOTH = 0.1
STILL_TICKS = 6  # 100 ms at 60 Hz without a new frame in a match: paused
# Scene.ebc.stage_id is a signed 32-bit IntProperty: the game's u32 is shown without its top bit.
STAGE_ID_PROPERTY_MASK = 0x7FFFFFFF  # exponential smoothing of the times shown in the Connection panel

UNREPRESENTABLE = "Camera does not look at the stage plane: the game keeps the last valid aim"

EPSILON = cameras.EPSILON

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
    return _state.error


def camera_warning() -> str | None:
    """Warning about the camera being sent (shown in the Camera panel)."""
    return _state.camera_warning


def io_summary() -> str | None:
    """'12 calls · 0.04 ms I/O · tick 0.31 ms' for the Connection panel (None before a tick)."""
    io = _state.io
    if io is None:
        return None
    return f"{io.calls} calls · {_state.io_ms:.2f} ms I/O · tick {_state.tick_ms:.2f} ms"


_stage_listener = None  # set by ui.stages (it imports this module, not the other way round)


def on_stage_changed(fn) -> None:
    """``fn(s, scene, poll_result)`` is called after a poll that identified (or lost) the stage."""
    global _stage_listener
    _stage_listener = fn


def pause_message() -> str | None:
    """What the last Pause at frame did, for the Timeline panel (None before one)."""
    return _state.pause_message


def request_cut() -> None:
    """Send the next tick as a cut (clean-cut recipe), even if the camera did not change."""
    _state.cut_requested = True


def start() -> bool:
    if not runtime.get_game().connected:
        _state.error = "Not connected to Dolphin"
        return False
    _state.restart()
    if not is_running():
        bpy.app.timers.register(_timer_callback, first_interval=0.0, persistent=False)
    return True


def stop() -> None:
    if is_running():
        bpy.app.timers.unregister(_timer_callback)
    game = runtime.get_game()
    if game.ortho_active and game.connected:
        game.restore_projection()  # the game's perspective back: nothing drives the ortho camera now
    util.tag_redraw()


def _timer_callback():
    try:
        keep_going = tick(bpy.context.scene)
    except Exception as exc:  # never raise from a timer: the sync stops and says why  # noqa: BLE001
        traceback.print_exc()
        _state.error = f"Sync stopped: {exc}"
        keep_going = False
    if not keep_going:
        util.tag_redraw()
        return None
    return INTERVAL


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
        _state.error = "Disconnected from Dolphin"
        return False
    t0 = time.perf_counter()
    io0 = game.io.copy()
    s = props.settings_of(scene)
    _state.ticks += 1

    result = game.poll()  # the only frame-counter read of the tick
    if result.frame is None:
        _state.error = game.last_error or "Could not read the game frame"
        return False
    frame = result.frame
    _show_poll(s, scene, result)
    stage = stage_presets.effective_overrides(s)  # after the poll: the row of the stage identified now
    _pause_at_frame(result, stage.pause_frame)

    _sync_timeline(s, scene, frame)
    camera = effective_camera(s, scene)
    if s.camera_direction == "BLENDER_TO_GAME":
        clip_from_objects(s, camera, stage)
    # After the timeline: drivers and keyframes of this frame are evaluated.
    props.push_changed(s)

    origin = effective_origin(s, camera)
    plan = _plan_send(s, scene, game, camera, origin)
    positions, sample = _memory_phase(s, game, frame, plan)
    if positions is not None:
        _sync_players(s, positions)
    if sample is not None:
        apply_game_camera(sample, camera, origin, scene)
    elif plan.pose is not None:
        show_aim(origin, camera, plan.pose)

    if _state.ticks % DEVSERVER_REFRESH_TICKS == 0 and runtime.devserver.available:
        status = runtime.devserver.status()
        runtime.note_running(None if status is None else status.running)
    _track_running(result)
    _state.last_frame = frame
    _record_timing(game, io0, t0)
    return True


def _track_running(result) -> None:
    """Running or paused for the Play / Pause button, from the frame counter this tick read anyway:
    it moved = running; still for STILL_TICKS ticks in a match = paused (menus say nothing)."""
    if not result.in_match:
        _state.still_ticks = 0
        return
    if result.frame != _state.last_frame:
        _state.still_ticks = 0
        runtime.note_running(True)
        runtime.get_game().debug.note_running()  # nor debug-paused (Start in game, Code Menu)
        return
    _state.still_ticks += 1
    if _state.still_ticks >= STILL_TICKS:
        runtime.note_running(False)


def _show_poll(s, scene, result) -> None:
    """The frame and stage id in the panel; the stage listener on a stage change."""
    if s.frame_number != result.frame:
        s.frame_number = result.frame
    if result.stage_id is not None and s.stage_id != (result.stage_id & STAGE_ID_PROPERTY_MASK):
        s.stage_id = result.stage_id & STAGE_ID_PROPERTY_MASK
    if result.stage_changed and _stage_listener is not None:  # new match, Connect, reload: the stage preset
        _stage_listener(s, scene, result)


def _pause_at_frame(result, target: int) -> None:
    """Pause at frame (global, or the stage row's): once when the match frame crosses ``target``."""
    trigger = _state.pause_at
    if result.new_match or result.stage_changed:
        trigger.reset()
    if trigger.check(result.frame, result.in_match, target):
        request_pause(f"frame {target}")


def request_pause(reason: str = "") -> bool:
    """Pause the emulation now: the one entry point of Pause at frame.

    With Game pause (debug mode) on: P+'s debug pause, a memory write (the game stops on this frame,
    Dolphin keeps running). Else the emulator control: the DevServer when it answers (the pause lands
    on this frame), else Dolphin's Frame Advance hotkey on EBC's DSU pad (sent at once; Dolphin may
    stop 1-2 frames later). Nothing while a frame-by-frame recording drives the emulator. The
    outcome goes to ``pause_message()``.
    """
    what = f"Pause at {reason}" if reason else "Pause"
    if record.drives_emulator():
        _state.pause_message = f"{what}: skipped, the frame-by-frame recording drives the emulator"
        return False
    if props.settings(bpy.context).game_pause:
        debug = runtime.get_game().debug
        ok = debug.pause()
        _state.pause_message = f"{what} (game pause)" if ok else f"{what} failed (game pause: {debug.last_error})"
        util.tag_redraw()
        return ok
    ctl = runtime.control()
    if ctl is None:
        _state.pause_message = f"{what}: no emulator control (DevServer or Dolphin hotkeys)"
        return False
    ok = _pause_with(ctl)
    if not ok and ctl is runtime.devserver and runtime.dsu.connected:  # the DevServer went away: hotkeys
        ctl = runtime.dsu_control
        ok = _pause_with(ctl)
    runtime.note_running(False if ok else None)
    name = "DevServer" if ctl is runtime.devserver else "Dolphin hotkeys"
    _state.pause_message = f"{what} ({name})" if ok else f"{what} failed ({name}: {ctl.last_error or 'no answer'})"
    util.tag_redraw()
    return ok


def _pause_with(ctl) -> bool:
    if isinstance(ctl, emucontrol.DSUControl):
        return ctl.pause(seen_running=True)  # the tick just saw the frame move
    return ctl.pause()


class Plan(NamedTuple):
    """What the memory phase of a tick does with the camera."""

    sending: bool  # Blender -> Brawl (not recording, not handed back)
    pose: cam_math.CameraPose | None  # what is sent (None: nothing representable yet)
    cut: bool
    receiving: bool  # Brawl -> Blender: the game camera is read


def _plan_send(s, scene, game: g.Game, camera, origin) -> Plan:
    """Blender side only: nothing is sent while recording, handed back, or in Brawl -> Blender."""
    recording = record.is_recording()
    sending = camera is not None and s.camera_direction == "BLENDER_TO_GAME" and not recording
    if sending and _hand_back(s, game):
        sending = False
    receiving = camera is not None and s.camera_direction == "GAME_TO_BLENDER" and not recording
    if not sending:
        _state.last_camera = None
        return Plan(False, None, False, receiving)
    pointer = camera.as_pointer()
    cut = _state.cut_requested or pointer != _state.last_camera
    _state.cut_requested = False
    _state.last_camera = pointer
    if cut:
        _state.cuts += 1
    return Plan(True, camera_pose(s, scene, camera, origin, free_target=game.native_freeze), cut, receiving)


def _memory_phase(s, game: g.Game, frame: int, plan: Plan):
    """Every memory access of the tick, in ``game.tick()``: (player positions or None, game camera
    sample or None)."""
    sending, pose = plan.sending, plan.pose
    positions = sample = None
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
        game.hold_intro_skip(pose)  # Skip stage intro camera: no I/O when off or once the fight runs
        if pose is not None:
            write_pose(s, game, pose, plan.cut)
        elif plan.receiving:
            sample = game.read_camera_sample(frame)
    return positions, sample


def _record_timing(game: g.Game, io0, t0: float) -> None:
    """Calls and time of this tick for the Connection panel (smoothed)."""
    io = game.io.since(io0)
    _state.io = io
    k = 1.0 if _state.ticks == 1 else SMOOTH
    _state.tick_ms += k * ((time.perf_counter() - t0) * 1000.0 - _state.tick_ms)
    _state.io_ms += k * (io.ns / 1e6 - _state.io_ms)


def _sync_timeline(s, scene, frame: int) -> None:
    if s.timeline_mode == "FOLLOW":
        want = frame + LOOKAHEAD
        if scene.frame_current != want:
            scene.frame_set(want)
    elif s.timeline_mode == "START_AT":
        last = _state.last_frame
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
        except Exception:  # no window for the operator: the timeline is left alone  # noqa: BLE001
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
        if not _state.handed_back:
            game.freeze_camera(False)
            _state.handed_back = True
        return True
    if _state.handed_back:
        _state.handed_back = False
        game.freeze_camera(s.lock_game_camera)
        _state.cut_requested = True
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


CLIP_RANGES = {"near_clip": (1, 1000), "far_clip": (1, 5000)}  # the sliders' ranges


def clip_depth(camera, obj) -> float | None:
    """Depth of ``obj`` in front of ``camera``: its distance along the view axis from the eye (what
    the game's near/far measure; a sideways offset does not count). None when it does not apply.

    A clip helper (``ebc.clip_helper``) follows the camera through a Child Of constraint without
    scale, so this is exactly minus its local Z, whatever the camera's scale."""
    if obj is None or obj == camera or camera is None or getattr(camera, "type", None) != "CAMERA":
        return None
    if camera.data.type != "PERSP":
        return None
    mw = camera.matrix_world
    forward = -mw.col[2].xyz.normalized()
    return (obj.matrix_world.translation - mw.translation).dot(forward)


def clip_value(prop: str, depth: float) -> int:
    """The slider value for a depth (clamped to the slider's range)."""
    lo, hi = CLIP_RANGES[prop]
    return round(min(max(depth, lo), hi))


def clip_from_objects(s, camera, stage: st.StageOverrides | None = None) -> None:
    """Near/Far objects: the clip planes follow their depth in front of the camera (``clip_depth``).

    The objects in force are the current stage row's when it sets them, else the Camera panel's
    (``stage``: ``stage_presets.effective_overrides``, computed when not given). Written through the
    panel properties, so their update callbacks send it and the sliders show it.
    """
    stage = stage or stage_presets.effective_overrides(s)
    for prop, obj in (("near_clip", stage.near_object), ("far_clip", stage.far_object)):
        depth = clip_depth(camera, obj)
        if depth is None:
            continue
        value = clip_value(prop, depth)
        if getattr(s, prop) != value:
            setattr(s, prop, value)


def game_freeze_is_code_menu() -> bool:
    """Keep origin on the stage plane only matters with the Code Menu lock (the game keeps the aim
    on the plane); with the native freeze the aim follows the view axis, the option does nothing."""
    return runtime.get_game().freeze_method == fz.FREEZE_CODE_MENU


def camera_pose(s, scene, camera, origin=None, free_target: bool = False):
    """The pose to send for ``camera`` (Blender side only, no memory access); None if none.

    ``free_target`` (native freeze): the target may leave the stage plane, so an axis that
    misses it is still sent (a point along it) instead of keeping the last target.
    An ORTHO camera gives an orthographic pose: same target and roll, bounds from its
    ortho_scale / sensor fit / shifts and the game aspect, eye pulled back along the axis.
    """
    if s.keep_origin_on_plane and origin is not None and game_freeze_is_code_menu():
        _keep_origin_on_plane(origin)
    proj = projection_camera(camera)
    ortho = is_ortho(proj)
    fov = None
    if s.sync_fov and camera.type == "CAMERA" and not ortho:
        fov = cameras.camera_vertical_fov(camera if proj is camera else proj, scene)
    mw = [tuple(row) for row in camera.matrix_world]
    # Measured: a positive game roll turns the picture counter-clockwise, a positive
    # Blender roll (right side up) clockwise: the pose carries the opposite roll.
    pose, ok = cam_math.pose_from_matrix(mw, fov, bool(s.copy_camera_roll), _state.last_target, free_target)
    _state.camera_warning = None if ok else UNREPRESENTABLE
    if pose is not None and ok:
        _state.last_target = pose.target
    if pose is not None and ortho:
        d = proj.data
        # the bounds follow from the game's aspect when the block is written (Game._project)
        lens = cam_math.OrthoLens(d.ortho_scale, d.sensor_fit, d.shift_x, d.shift_y)
        pose = cam_math.ortho_pose(pose, mw, lens)
    return pose


def write_pose(s, game: g.Game, pose, cut: bool = False) -> bool:
    """Send ``pose`` (the shake suppressed last when the option is on)."""
    return game.write_camera_pose(pose, cut=cut, suppress_shake=s.disable_screen_shake)


def send_camera(s, scene, game: g.Game, camera, origin=None, cut: bool = False) -> bool:
    """Blender -> Brawl once, outside the tick (tools, the paused screenshot): pose then write."""
    pose = camera_pose(s, scene, camera, origin, free_target=game.native_freeze)
    if pose is None:
        return False
    show_aim(origin, camera, pose)
    return write_pose(s, game, pose, cut)


def show_aim(origin, camera, pose) -> None:
    """Blender -> Brawl: put a plain-marker Origin on the aim point sent, so it marks what the
    game looks at, as Brawl -> Blender does. Left alone when the camera's placement reads it
    (Track To, parent, a blended camera's Track To) or when it is keyed or driven
    (``camera.origin_marks_aim``, docs/research/debug-camera.md)."""
    if not cam_math.origin_marks_aim(origin, camera):
        return
    if _differs(origin.matrix_world.translation, pose.target):
        origin.matrix_world.translation = pose.target


def receive_camera(game: g.Game, camera, origin, scene=None) -> bool:
    """Brawl -> Blender once: copy the rendered game camera (position, orientation, roll, FOV).

    A tracking constraint on the camera overrides the copied orientation;
    the Origin is moved onto the game's target so it still aims right.
    """
    sample = game.read_camera_sample()
    if sample is None:
        return False
    apply_game_camera(sample, camera, origin, scene)
    return True


def apply_game_camera(sample, camera, origin, scene=None) -> None:
    """Blender side of Brawl -> Blender: camera matrix, Origin on the target, lens from the FOV
    (or ORTHO and ortho_scale when the game is orthographic)."""
    mw = cameras.game_camera_matrix(sample)
    if not cameras.same_pose(camera, mw):
        cameras.set_pose_keep_scale(camera, mw)
    target = cam_math.to_blender(sample.target)
    if origin is not None and origin != camera and _differs(origin.matrix_world.translation, target):
        origin.matrix_world.translation = target
    cameras.apply_projection(camera, sample)
    if scene is not None and camera.type == "CAMERA" and camera.data.type == "PERSP":
        lens = cameras.lens_for(camera, scene, sample.fov)
        if math.isfinite(lens) and abs(camera.data.lens - lens) > EPSILON:
            camera.data.lens = lens
