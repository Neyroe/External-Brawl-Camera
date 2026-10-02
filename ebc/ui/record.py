"""Record the game camera into a Blender camera (keyframes), on the main thread.

The reading runs in ``core.recorder`` (a worker thread, no ``bpy``). This
module starts it, drains its queue from a ``bpy.app.timers`` callback and turns
each sample into keyframes: location + rotation (quaternion, kept continuous
with ``make_compatible``) on the object and ``lens`` on the camera data (``ortho_scale``
when the game is orthographic: the camera is switched to ORTHO), at
Blender frame = game frame (the frame the sample was rendered on, same
convention as Follow game frame). On stop the recorded keys are set to LINEAR.

Nothing from the worker touches ``bpy``: only ``drain()`` does, on the main
thread.
"""

from __future__ import annotations

import traceback

import bpy

from .. import runtime
from ..core.recorder import FrameStepRecorder, LiveRecorder, RecordStats
from . import sync

INTERVAL = 1.0 / 30.0
GAME_CAMERA_NAME = "EBC_GameCamera"

_rec: dict = {
    "worker": None,
    "camera": None,  # object name (survives undo better than a reference)
    "scene": None,
    "include_shake": False,
    "prev_quat": None,
    "frames": set(),
    "samples": {},  # frame -> CameraSample of the current/last recording (raw game values)
    "stats": None,  # RecordStats of the last recording (kept after stop for the panel)
    "error": None,
    "mode": None,
}


def is_recording() -> bool:
    worker = _rec["worker"]
    return worker is not None


def stats() -> RecordStats | None:
    worker = _rec["worker"]
    return worker.stats if worker is not None else _rec["stats"]


def last_error() -> str | None:
    worker = _rec["worker"]
    if worker is not None and worker.stats.error:
        return worker.stats.error
    return _rec["error"]


def keyframed_frames() -> int:
    return len(_rec["frames"])


def samples() -> dict:
    """Raw samples of the current or last recording, by game frame."""
    return _rec["samples"]


# -- start / stop ---------------------------------------------------------------------


def start(scene, camera, mode: str, include_shake: bool, end_frame: int = 0) -> str | None:
    """Start recording into ``camera``. Returns an error message, or None."""
    if is_recording():
        return "Already recording"
    game = runtime.get_game()
    if not game.connected:
        return "Not connected to Dolphin"
    p = game.p
    if mode == "STEP":
        client = runtime.devserver
        if client.status() is None:
            return f"Frame by frame needs the DevServer: {client.last_error or 'unreachable'}"
        worker = FrameStepRecorder(
            game.read_bytes, p.MATCH_FRAME.addr, p.CAM_RECORD_BASE, p.CAM_RECORD_SIZE, client, end_frame
        )
    else:
        worker = LiveRecorder(game.read_bytes, p.MATCH_FRAME.addr, p.CAM_RECORD_BASE, p.CAM_RECORD_SIZE, end_frame)
    camera.rotation_mode = "QUATERNION"
    _rec.update(
        worker=worker,
        camera=camera.name,
        scene=scene.name,
        include_shake=include_shake,
        prev_quat=None,
        frames=set(),
        samples={},
        stats=None,
        error=None,
        mode=mode,
    )
    worker.start()
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=INTERVAL, persistent=False)
    return None


def stop() -> None:
    """Stop the worker, insert what is left, set the recorded keys to LINEAR."""
    worker = _rec["worker"]
    if worker is None:
        return
    worker.stop()
    try:
        drain()
    finally:
        _rec["stats"] = worker.stats
        _rec["error"] = worker.stats.error
        _rec["worker"] = None
        _finalize()
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
        sync._tag_redraw()


def _timer():
    try:
        drain()
        worker = _rec["worker"]
        if worker is not None and not worker.running:
            stop()  # end frame reached or error
            return None
    except Exception as exc:
        traceback.print_exc()
        _rec["error"] = f"Recording stopped: {exc}"
        stop()
        return None
    sync._tag_redraw()
    return INTERVAL if _rec["worker"] is not None else None


# -- keyframes ----------------------------------------------------------------------------


def _objects():
    obj = bpy.data.objects.get(_rec["camera"] or "")
    scene = bpy.data.scenes.get(_rec["scene"] or "")
    return obj, scene


def drain() -> int:
    """Insert the queued samples as keyframes (main thread). Returns how many."""
    worker = _rec["worker"]
    if worker is None:
        return 0
    samples = worker.drain()
    obj, scene = _objects()
    if obj is None or scene is None:
        return 0
    n = 0
    for sample in samples:
        insert_sample(obj, scene, sample, _rec["include_shake"])
        _rec["frames"].add(sample.frame)
        _rec["samples"][sample.frame] = sample
        n += 1
    return n


def insert_sample(obj, scene, sample, include_shake: bool = False) -> None:
    """Pose ``obj`` like the game camera of ``sample`` and keyframe it at frame ``sample.frame``."""
    sync.set_pose_keep_scale(obj, sync.game_camera_matrix(sample, include_shake))
    quat = obj.rotation_quaternion.copy()
    prev = _rec["prev_quat"]
    if prev is not None:
        quat.make_compatible(prev)  # no sign flip between two keys
        obj.rotation_quaternion = quat
    _rec["prev_quat"] = quat.copy()
    frame = sample.frame
    obj.keyframe_insert("location", frame=frame, group="EBC Record")
    obj.keyframe_insert("rotation_quaternion", frame=frame, group="EBC Record")
    if obj.type != "CAMERA":
        return
    sync.apply_projection(obj, sample)  # ORTHO + ortho_scale when the game is orthographic
    if obj.data.type == "ORTHO":
        obj.data.keyframe_insert("ortho_scale", frame=frame)
    elif obj.data.type == "PERSP":
        obj.data.lens = sync.lens_for(obj, scene, sample.fov)
        obj.data.keyframe_insert("lens", frame=frame)


def action_fcurves(id_data):
    """F-curves of an ID's action, on Blender 4.2 to 5.x (layered actions)."""
    anim = getattr(id_data, "animation_data", None)
    action = anim.action if anim is not None else None
    if action is None:
        return []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None and len(legacy):
        return list(legacy)
    try:
        from bpy_extras import anim_utils

        slot = getattr(anim, "action_slot", None)
        bag = anim_utils.action_get_channelbag_for_slot(action, slot)
        return list(bag.fcurves) if bag is not None else []
    except Exception:
        return list(legacy or [])


def set_interpolation(id_data, paths, frames, interpolation: str, easing: str | None = None) -> int:
    """Set the interpolation of the keys of ``paths`` on ``frames`` (a set, or None = all)."""
    n = 0
    for fc in action_fcurves(id_data):
        if fc.data_path not in paths:
            continue
        for kp in fc.keyframe_points:
            if frames is None or round(kp.co.x) in frames:
                kp.interpolation = interpolation
                if easing is not None:
                    kp.easing = easing
                n += 1
        fc.update()
    return n


def _finalize() -> None:
    obj, _scene = _objects()
    frames = _rec["frames"]
    if obj is None or not frames:
        return
    set_interpolation(obj, {"location", "rotation_quaternion"}, frames, "LINEAR")
    if obj.type == "CAMERA":
        set_interpolation(obj.data, {"lens", "ortho_scale"}, frames, "LINEAR")
