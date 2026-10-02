import math
import os
import tempfile

import bpy
from bpy.props import EnumProperty, IntProperty
from bpy.types import Operator
from mathutils import Matrix

from .. import runtime
from ..core import camera as cam_math
from ..core import game as g
from . import props, record, sync

RIG_COLLECTION = "EBC"
# Default framing: in front of the stage. Brawl +Z (towards the viewer) is Blender +Y.
RIG_CAMERA_LOCATION = (0.0, 150.0, 20.0)


def _report_game_error(op: Operator, what: str) -> None:
    err = runtime.get_game().last_error
    op.report({"WARNING"}, f"{what}: {err}" if err else what)


def dolphin_process_count() -> int | None:
    """Number of running Dolphin processes (Linux /proc only), None if unknown.

    Dolphin Memory Engine hooks the FIRST one it finds: with two Dolphins
    open it may pick the wrong one.
    """
    if not os.path.isdir("/proc"):
        return None
    n = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                    name = f.read().strip().lower()
            except OSError:
                continue
            if name.startswith("dolphin-emu") or name.startswith("project-plus"):
                n += 1
    except OSError:
        return None
    return n


class EBC_OT_connect(Operator):
    """Hook Dolphin Memory Engine to the running Dolphin"""

    bl_idname = "ebc.connect"
    bl_label = "Connect to Dolphin"

    def execute(self, context):
        game = runtime.get_game()
        s = props.settings(context)
        game.requested_profile = s.game_version
        if not game.connect():
            msg = getattr(game.backend, "notice", None) or "Could not find Dolphin. Is the game running?"
            backend_error = getattr(game.backend, "import_error", None)
            if backend_error:
                msg = f"dolphin_memory_engine unavailable: {backend_error}"
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        props.push_settings(s)
        runtime.devserver.set_port(s.devserver_port)
        runtime.devserver.status()
        s.frame_number = game.current_frame() or 0
        count = dolphin_process_count()
        notice = getattr(game.backend, "notice", None)
        if count is not None and count > 1:
            self.report({"WARNING"}, f"{count} Dolphin processes running: EBC hooked the first one found")
        elif notice:
            self.report({"WARNING"}, notice)
        elif not game.profile_sure:
            self.report({"WARNING"}, f"Game version: {game.profile_info}")
        else:
            self.report({"INFO"}, f"Connected to Dolphin: {game.profile_info or game.p.label}")
        return {"FINISHED"}


class EBC_OT_disconnect(Operator):
    """Stop the sync, put back every game value EBC changed, and release Dolphin"""

    bl_idname = "ebc.disconnect"
    bl_label = "Disconnect"

    def execute(self, context):
        sync.stop()
        if not runtime.get_game().disconnect():
            self.report({"WARNING"}, "Some game values could not be restored")
        runtime.devserver.close()
        return {"FINISHED"}


class EBC_OT_sync_start(Operator):
    """Start synchronising Blender and the game (~60 times per second)"""

    bl_idname = "ebc.sync_start"
    bl_label = "Start sync"

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected and not sync.is_running()

    def execute(self, context):
        if not sync.start():
            self.report({"ERROR"}, sync.last_error() or "Could not start the sync")
            return {"CANCELLED"}
        return {"FINISHED"}


class EBC_OT_sync_stop(Operator):
    """Stop the synchronisation"""

    bl_idname = "ebc.sync_stop"
    bl_label = "Stop sync"

    @classmethod
    def poll(cls, context):
        return sync.is_running()

    def execute(self, context):
        sync.stop()
        return {"FINISHED"}


class EBC_OT_reapply(Operator):
    """Send every setting of the panel to the game again"""

    bl_idname = "ebc.reapply"
    bl_label = "Re-apply settings"

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected

    def execute(self, context):
        props.push_settings(props.settings(context), force=True)
        return {"FINISHED"}


class EBC_OT_isolate_fighter(Operator):
    """Show only this character on a pure key colour: hide the other three and the stage, turn the HUD off
    and Full-screen background on, opaque. Press again to put everything back as it was"""

    bl_idname = "ebc.isolate_fighter"
    bl_label = "Isolate character"
    bl_options = {"REGISTER", "UNDO"}

    slot: IntProperty(name="Slot", description="Port to isolate (0 = P1)", min=0, max=3, default=0)

    def execute(self, context):
        s = props.settings(context)
        modes = s.fighter_modes()
        new_modes, isolating = g.toggle_isolation(modes, self.slot)
        if isolating and g.isolated_slot(modes) is None:
            # entering isolation (not switching to another slot): remember what to put back
            s.isolate_hud = s.hud
            s.isolate_green_screen = s.green_screen
            s.isolate_stage = s.stage_display
            s.isolate_saved = True  # from now on the full-screen background is sent opaque
        for slot, mode in zip(props.SLOTS, new_modes, strict=True):
            tint = s.tint(slot)
            if tint.mode != mode:
                tint.mode = mode  # its update writes the game
        if isolating:
            # Opaque fill (whatever the swatch's alpha) over a hidden stage: the key colour is exact
            # everywhere, foreground stage parts included (they go with the stage).
            s.green_screen = True
            props.update_green_screen(s)
            s.stage_display = g.STAGE_HIDDEN
            s.hud = False
        elif s.isolate_saved:
            s.isolate_saved = False  # the swatch's own alpha again
            s.hud = s.isolate_hud
            s.stage_display = s.isolate_stage
            s.green_screen = s.isolate_green_screen
            props.update_green_screen(s)
        return {"FINISHED"}


def _rig_collection(context):
    coll = bpy.data.collections.get(RIG_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(RIG_COLLECTION)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def _new_empty(name, coll, display="PLAIN_AXES", size=10.0):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = display
    obj.empty_display_size = size
    coll.objects.link(obj)
    return obj


def _track(camera, target) -> None:
    for con in camera.constraints:
        if con.type in sync.ORIGIN_CONSTRAINTS:
            con.target = target
            return
    con = camera.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"


def default_fov() -> float:
    """The stage's own vertical FOV (radians) as last read from the game, else 30 degrees."""
    fov = runtime.get_game().stage_fov
    return fov if fov is not None else math.radians(g.GAME_DEFAULT_FOV_DEG)


def set_default_fov(camera, scene, fov: float | None = None) -> None:
    cam = camera.data
    if cam.type != "PERSP":
        return
    r = scene.render
    cam.lens = g.lens_for_vertical_fov(
        default_fov() if fov is None else fov,
        cam.sensor_width,
        cam.sensor_height,
        cam.sensor_fit,
        r.resolution_x * r.pixel_aspect_x,
        r.resolution_y * r.pixel_aspect_y,
    )


def new_camera(context, name, origin):
    coll = _rig_collection(context)
    data = bpy.data.cameras.new(name)
    data.clip_start = 1.0
    data.clip_end = 5000.0
    cam = bpy.data.objects.new(name, data)
    cam.location = RIG_CAMERA_LOCATION
    coll.objects.link(cam)
    set_default_fov(cam, context.scene)
    if origin is not None:
        _track(cam, origin)
    return cam


class EBC_OT_create_rig(Operator):
    """Create the missing Origin, Camera and PLAYER_1..4 objects and assign them"""

    bl_idname = "ebc.create_rig"
    bl_label = "Create EBC rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        coll = _rig_collection(context)
        created = []
        if s.origin is None:
            s.origin = bpy.data.objects.get("Origin") or _new_empty("Origin", coll, "SPHERE", 5.0)
            created.append(s.origin.name)
        if s.camera is None:
            existing = bpy.data.objects.get("Camera")
            if existing is not None and existing.type == "CAMERA":
                s.camera = existing
            else:
                s.camera = new_camera(context, "Camera", s.origin)
                created.append(s.camera.name)
        for slot in props.SLOTS:
            attr = f"player_{slot + 1}"
            if getattr(s, attr) is None:
                name = f"PLAYER_{slot + 1}"
                obj = bpy.data.objects.get(name) or _new_empty(name, coll, "SINGLE_ARROW", 10.0)
                setattr(s, attr, obj)
                created.append(obj.name)
        if context.scene.camera is None:
            context.scene.camera = s.camera
        self.report({"INFO"}, f"Created: {', '.join(created)}" if created else "Rig already complete")
        return {"FINISHED"}


class EBC_OT_reset_fov(Operator):
    """Set the synced camera's focal length to the stage's default field of view
    (30 degrees vertical when the stage value was never read). Works offline"""

    bl_idname = "ebc.reset_fov"
    bl_label = "Reset FOV"

    @classmethod
    def poll(cls, context):
        cam = sync.effective_camera(props.settings(context), context.scene)
        return cam is not None and cam.type == "CAMERA"

    def execute(self, context):
        cam = sync.effective_camera(props.settings(context), context.scene)
        set_default_fov(cam, context.scene)
        return {"FINISHED"}


AXONOMETRIC_PRESETS = {
    "ISOMETRIC": ("Isometric", cam_math.ELEVATION_ISOMETRIC),
    "DIMETRIC": ("Dimetric 2:1", cam_math.ELEVATION_DIMETRIC),
}


def aimed_point(camera):
    """What the camera looks at: its tracking target, else its view axis on the stage plane
    (else 100 units ahead)."""
    for con in camera.constraints:
        if con.type in sync.ORIGIN_CONSTRAINTS and con.enabled and getattr(con, "target", None) is not None:
            return tuple(con.target.matrix_world.translation)
    mw = [tuple(row) for row in camera.matrix_world]
    return cam_math.plane_target(mw) or cam_math.axis_target(mw)


class EBC_OT_camera_axonometric(Operator):
    """Turn the synced camera into an orthographic axonometric view of what it looks at"""

    bl_idname = "ebc.camera_axonometric"
    bl_label = "Axonometric view"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="View",
        items=[
            ("ISOMETRIC", "Isometric", "True isometric: elevation 35.264°, rotation (54.736°, 0°, yaw)"),
            ("DIMETRIC", "Dimetric 2:1", "2:1 pixel-art dimetric: elevation 30°, rotation (60°, 0°, yaw)"),
        ],
        default="ISOMETRIC",
    )

    @classmethod
    def description(cls, context, properties):
        if properties.preset == "DIMETRIC":
            return (
                "Orthographic 2:1 dimetric view (elevation 30°, floor edges 2 pixels across for 1 up) of the "
                "point the synced camera looks at, from the chosen yaw"
            )
        return "Orthographic true isometric view (elevation 35.264°) of the point the synced camera looks at"

    @classmethod
    def poll(cls, context):
        return props.synced_camera(props.settings(context)) is not None

    def execute(self, context):
        s = props.settings(context)
        cam = props.synced_camera(s)
        if cam is None:
            self.report({"WARNING"}, "No synced camera")
            return {"CANCELLED"}
        label, elevation = AXONOMETRIC_PRESETS[self.preset]
        azimuth = float(s.axonometric_azimuth)
        target = aimed_point(cam)
        eye = cam.matrix_world.translation
        distance = max(1.0, sum((eye[i] - target[i]) ** 2 for i in range(3)) ** 0.5)
        if cam.data.type != "ORTHO":
            props.keep_framing_ortho(cam, context.scene, distance)  # the perspective it had, at its target
            cam.data.type = "ORTHO"
        sync.set_pose_keep_scale(cam, Matrix(cam_math.axonometric_matrix(target, elevation, azimuth, distance)))
        sync.request_cut()
        self.report({"INFO"}, f"{cam.name}: {label}, yaw {azimuth:g}°, ortho scale {cam.data.ortho_scale:.1f}")
        return {"FINISHED"}


class EBC_OT_camera_add(Operator):
    """Create a new camera tracking the origin and add it to the list"""

    bl_idname = "ebc.camera_add"
    bl_label = "Add camera"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        cam = new_camera(context, f"EBC_Camera.{len(s.cameras) + 1:03d}", s.origin)
        item = s.cameras.add()
        item.camera = cam
        s.cameras_index = len(s.cameras) - 1
        return {"FINISHED"}


class EBC_OT_camera_remove(Operator):
    """Remove the selected camera from the list (the object is kept)"""

    bl_idname = "ebc.camera_remove"
    bl_label = "Remove camera"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = props.settings(context)
        return 0 <= s.cameras_index < len(s.cameras)

    def execute(self, context):
        s = props.settings(context)
        s.cameras.remove(s.cameras_index)
        s.cameras_index = max(0, min(s.cameras_index, len(s.cameras) - 1))
        return {"FINISHED"}


class EBC_OT_camera_activate(Operator):
    """Make this camera the active one: the game camera cuts to it on the next sync tick"""

    bl_idname = "ebc.camera_activate"
    bl_label = "Activate camera"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", description="Row in the Cameras list, -1 for the Rig camera", default=-1)

    @classmethod
    def description(cls, context, properties):
        if properties.index < 0:
            return "Go back to the Rig camera: the game camera cuts to it on the next sync tick"
        return cls.__doc__

    def execute(self, context):
        s = props.settings(context)
        if self.index < 0:
            cam = s.camera
        elif self.index < len(s.cameras):
            cam = s.cameras[self.index].camera
            s.cameras_index = self.index
        else:
            cam = None
        if cam is None:
            self.report({"WARNING"}, "No camera to activate")
            return {"CANCELLED"}
        context.scene.camera = cam
        sync.request_cut()  # clean-cut recipe on the next tick, even for the same camera
        return {"FINISHED"}


class EBC_OT_camera_bind_marker(Operator):
    """Bind the selected camera to a timeline marker at the current frame"""

    bl_idname = "ebc.camera_bind_marker"
    bl_label = "Bind to marker"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = props.settings(context)
        return 0 <= s.cameras_index < len(s.cameras) and s.cameras[s.cameras_index].camera is not None

    def execute(self, context):
        s = props.settings(context)
        scene = context.scene
        cam = s.cameras[s.cameras_index].camera
        frame = scene.frame_current
        marker = next((m for m in scene.timeline_markers if m.frame == frame), None)
        if marker is None:
            marker = scene.timeline_markers.new(cam.name, frame=frame)
        marker.camera = cam
        scene.camera = cam
        self.report({"INFO"}, f"{cam.name} bound to frame {frame}")
        return {"FINISHED"}


class EBC_OT_match_game_aspect(Operator):
    """Set the render resolution to the game's aspect (keeps the height, adjusts the width and the pixel
    aspect) so the Blender framing matches the game's horizontally too"""

    bl_idname = "ebc.match_game_aspect"
    bl_label = "Match game aspect"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        aspect = runtime.get_game().aspect or g.GAME_DEFAULT_ASPECT
        r = context.scene.render
        r.resolution_x, r.pixel_aspect_x, r.pixel_aspect_y = g.aspect_settings(aspect, r.resolution_y)
        self.report({"INFO"}, f"Render {r.resolution_x} x {r.resolution_y}, aspect {aspect:.4f}")
        return {"FINISHED"}


# -- Record ---------------------------------------------------------------------------------


def game_camera(context, create: bool = True):
    """The camera the recording goes to: the one picked, else EBC_GameCamera (created, listed)."""
    s = props.settings(context)
    cam = s.record_camera
    if cam is None:
        existing = bpy.data.objects.get(record.GAME_CAMERA_NAME)
        if existing is not None and existing.type == "CAMERA":
            cam = existing
        elif create:
            cam = new_camera(context, record.GAME_CAMERA_NAME, None)
        else:
            return None
        s.record_camera = cam
    if all(item.camera != cam for item in s.cameras):
        s.cameras.add().camera = cam
    return cam


class EBC_OT_record_start(Operator):
    """Record the game camera into the chosen camera (keyframes at Blender frame = game frame)"""

    bl_idname = "ebc.record_start"
    bl_label = "Record"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected and not record.is_recording()

    def execute(self, context):
        s = props.settings(context)
        if s.record_mode == "STEP":
            runtime.devserver.set_port(s.devserver_port)
            if runtime.devserver.status() is None:
                self.report({"WARNING"}, f"Frame by frame needs the DevServer: {runtime.devserver.last_error}")
                return {"CANCELLED"}
        cam = game_camera(context)
        if cam.constraints:
            self.report({"WARNING"}, f"{cam.name} has constraints: they override the recorded rotation")
        if s.lock_game_camera:
            s.lock_game_camera = False  # the game camera must move by itself
            self.report({"INFO"}, "Lock game camera turned off to record the game's own camera")
        err = record.start(context.scene, cam, s.record_mode, s.record_include_shake, s.record_end_frame)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class EBC_OT_record_stop(Operator):
    """Stop recording; the recorded keys are set to linear interpolation"""

    bl_idname = "ebc.record_stop"
    bl_label = "Stop recording"

    @classmethod
    def poll(cls, context):
        return record.is_recording()

    def execute(self, context):
        record.stop()
        st = record.stats()
        if st is not None:
            self.report({"INFO"}, f"Recorded {st.captured} frames, {st.missed} missed, {st.repeated} repeated")
        return {"FINISHED"}


# -- Transitions ------------------------------------------------------------------------------

BLEND_NAME = sync.BLEND_NAME
CON_FROM = sync.CON_FROM
CON_TO = sync.CON_TO


def _add_driver_var(driver, name, id_type, id_, path):
    var = driver.variables.new()
    var.name = name
    var.type = "SINGLE_PROP"
    var.targets[0].id_type = id_type
    var.targets[0].id = id_
    var.targets[0].data_path = path


def _bind_marker(scene, frame, cam):
    marker = next((m for m in scene.timeline_markers if m.frame == frame), None)
    if marker is None:
        marker = scene.timeline_markers.new(cam.name, frame=frame)
    marker.camera = cam


def build_transition(context, src, dst, start: int, frames: int, easing: str):
    """EBC_Blend camera: Copy Transforms of ``src`` then of ``dst`` whose influence goes 0 -> 1.

    The constraint influence blends the two world matrices (location
    interpolated, rotation slerped); the lens follows through a driver. The
    blend camera is listed and bound to a marker at ``start``, ``dst`` at the
    end, so the synced camera is EBC_Blend during the transition.

    Two ORTHO cameras: EBC_Blend is ORTHO and its ortho_scale is driven from A's to B's.
    An ORTHO and a PERSP camera: the projection switches at mid-transition (``sync.projection_camera``);
    EBC_Blend stays PERSP with the perspective end's lens.
    """
    scene = context.scene
    s = props.settings(context)
    coll = _rig_collection(context)
    data = bpy.data.cameras.new(BLEND_NAME)
    for attr in ("sensor_width", "sensor_height", "sensor_fit", "clip_start", "clip_end", "shift_x", "shift_y"):
        setattr(data, attr, getattr(src.data, attr))
    ortho_a, ortho_b = sync.is_ortho(src), sync.is_ortho(dst)
    blend = bpy.data.objects.new(BLEND_NAME, data)
    coll.objects.link(blend)
    c_from = blend.constraints.new("COPY_TRANSFORMS")
    c_from.name, c_from.target = CON_FROM, src
    c_to = blend.constraints.new("COPY_TRANSFORMS")
    c_to.name, c_to.target = CON_TO, dst
    path = f'constraints["{CON_TO}"].influence'
    end = start + frames
    for frame, value in ((start, 0.0), (end, 1.0)):
        c_to.influence = value
        blend.keyframe_insert(path, frame=frame, group="EBC Transition")
    record.set_interpolation(blend, {path}, None, easing, None if easing in ("BEZIER", "LINEAR") else "EASE_IN_OUT")
    # lens: from src to dst, dst's lens expressed for src's sensor (the blend camera's)
    fc = data.driver_add("lens")
    drv = fc.driver
    drv.type = "SCRIPTED"
    _add_driver_var(drv, "a", "CAMERA", src.data, "lens")
    _add_driver_var(drv, "sa", "CAMERA", src.data, "sensor_width")
    _add_driver_var(drv, "b", "CAMERA", dst.data, "lens")
    _add_driver_var(drv, "sb", "CAMERA", dst.data, "sensor_width")
    _add_driver_var(drv, "t", "OBJECT", blend, path)
    if ortho_a == ortho_b:
        drv.expression = "a + (b * sa / sb - a) * t"  # a simple expression: no Python needed
    else:  # projection switch at mid-transition: the perspective end's lens throughout
        drv.expression = "b * sa / sb" if ortho_a else "a"
    if ortho_a and ortho_b:
        data.type = "ORTHO"
        fc = data.driver_add("ortho_scale")
        drv = fc.driver
        drv.type = "SCRIPTED"
        _add_driver_var(drv, "a", "CAMERA", src.data, "ortho_scale")
        _add_driver_var(drv, "b", "CAMERA", dst.data, "ortho_scale")
        _add_driver_var(drv, "t", "OBJECT", blend, path)
        drv.expression = "a + (b - a) * t"
    s.cameras.add().camera = blend
    _bind_marker(scene, start, blend)
    _bind_marker(scene, end, dst)
    return blend


class EBC_OT_transition_add(Operator):
    """Blend from the recorded game camera to the selected camera over N frames from the current frame
    (EBC_Blend camera, bound to timeline markers)"""

    bl_idname = "ebc.transition_add"
    bl_label = "Add transition"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        game_cam = game_camera(context, create=False)
        if game_cam is None:
            self.report({"WARNING"}, "Record the game camera first (Record panel)")
            return {"CANCELLED"}
        other = s.cameras[s.cameras_index].camera if 0 <= s.cameras_index < len(s.cameras) else None
        if other is None or other == game_cam or other.name.startswith(BLEND_NAME):
            self.report({"WARNING"}, "Select the camera to blend with in the Cameras list")
            return {"CANCELLED"}
        src, dst = (other, game_cam) if s.transition_reverse else (game_cam, other)
        start = context.scene.frame_current
        blend = build_transition(context, src, dst, start, s.transition_frames, s.transition_easing)
        context.scene.camera = blend  # what the start marker binds anyway
        msg = f"{blend.name}: {src.name} -> {dst.name}, frames {start}-{start + s.transition_frames}"
        if sync.is_ortho(src) != sync.is_ortho(dst):
            msg += f"; projection switches at frame {start + s.transition_frames // 2} (mid-transition)"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class EBC_OT_hand_back(Operator):
    """Keyframe "Hand back to the game" on at the current frame (off on the frame before): from there the
    game takes its camera back, easing from the last camera sent"""

    bl_idname = "ebc.hand_back"
    bl_label = "Hand back here"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        frame = context.scene.frame_current
        for f, value in ((frame - 1, False), (frame, True)):
            s.hand_back_to_game = value
            s.keyframe_insert("hand_back_to_game", frame=f, group="EBC")
        record.set_interpolation(context.scene, {"ebc.hand_back_to_game"}, None, "CONSTANT")
        self.report({"INFO"}, f"Hand back to the game at frame {frame}")
        return {"FINISHED"}


def state_path(slot: str) -> str:
    """Savestate file for a slot, in the extension's user directory."""
    try:
        base = bpy.utils.extension_path_user((__package__ or "").rpartition(".ui")[0], path="states", create=True)
    except Exception:  # not installed as an extension (dev checkout)
        base = os.path.join(tempfile.gettempdir(), "ebc_states")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"slot{slot}.sav")


class EBC_OT_devserver(Operator):
    """Control the emulator through the Dolphin DevServer"""

    bl_idname = "ebc.devserver"
    bl_label = "Emulator control"

    action: EnumProperty(
        items=[
            ("CHECK", "Check", "Check whether the DevServer answers"),
            ("PAUSE", "Pause", "Pause the emulation"),
            ("RESUME", "Resume", "Resume the emulation"),
            ("STEP", "Next frame", "Run until the game frame counter moves, then pause (approximate)"),
            ("SAVE", "Save state", "Save a state in the selected slot"),
            ("LOAD", "Load state", "Load the state of the selected slot"),
        ],
        default="CHECK",
    )

    def execute(self, context):
        s = props.settings(context)
        client = runtime.devserver
        client.set_port(s.devserver_port)
        if self.action == "CHECK":
            ok = client.status() is not None
        elif self.action == "PAUSE":
            ok = client.pause()
        elif self.action == "RESUME":
            ok = client.resume()
        elif self.action == "STEP":
            ok = client.advance_frame(runtime.get_game().current_frame)
        elif self.action == "SAVE":
            # a savestate keeps the RAM: never one in orthographic (the next sync tick sets it again)
            runtime.get_game().restore_projection()
            ok = client.save_state(state_path(s.state_slot))
        else:
            runtime.get_game().restore_projection()
            ok = client.load_state(state_path(s.state_slot))
            if ok:
                # a savestate brings its own RAM back: Code Menu values (the camera lock), colours...
                runtime.get_game().reapply_persistent()
        if not ok:
            self.report({"WARNING"}, f"DevServer: {client.last_error or 'unreachable'}")
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (
    EBC_OT_connect,
    EBC_OT_disconnect,
    EBC_OT_sync_start,
    EBC_OT_sync_stop,
    EBC_OT_reapply,
    EBC_OT_isolate_fighter,
    EBC_OT_create_rig,
    EBC_OT_reset_fov,
    EBC_OT_camera_axonometric,
    EBC_OT_camera_add,
    EBC_OT_camera_remove,
    EBC_OT_camera_activate,
    EBC_OT_camera_bind_marker,
    EBC_OT_match_game_aspect,
    EBC_OT_record_start,
    EBC_OT_record_stop,
    EBC_OT_transition_add,
    EBC_OT_hand_back,
    EBC_OT_devserver,
)
