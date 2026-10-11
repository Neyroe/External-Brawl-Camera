import os
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator
from mathutils import Matrix

from .. import runtime
from ..core import backend, dolphin_config, emucontrol
from ..core import camera as cam_math
from ..core import display as disp
from ..core import fighters as ft
from . import cameras, lifecycle, props, record, screenshot, sync, util


def dolphin_process_count() -> int | None:
    """Number of running Dolphin processes (Linux /proc only), None if unknown.

    Dolphin Memory Engine hooks the FIRST one it finds: with two Dolphins
    open it may pick the wrong one.
    """
    if not os.path.isdir("/proc"):
        return None
    return len(dolphin_config.dolphin_pids(dme_names()))


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
        dsu_error = start_dsu()
        s.frame_number = game.current_frame() or 0
        count = dolphin_process_count()
        notice = getattr(game.backend, "notice", None)
        if count is not None and count > 1:
            self.report({"WARNING"}, f"{count} Dolphin processes running: EBC hooked the first one found")
        elif notice:
            self.report({"WARNING"}, notice)
        elif dsu_error:
            self.report({"WARNING"}, f"Connected; Dolphin hotkeys unavailable: {dsu_error}")
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
        if not lifecycle.shutdown():
            self.report({"WARNING"}, "Some game values could not be restored")
        return {"FINISHED"}


def dme_names() -> tuple[str, ...]:
    """Process names DME hooks (the one it is bound to, else its defaults and the known builds)."""
    bound = getattr(runtime.get_game().backend, "bound_name", None) or os.environ.get(backend.PROCESS_NAME_ENV)
    return (bound,) if bound else backend.KNOWN_NAMES


def start_dsu() -> str | None:
    """Start EBC's DSU pad and find the running Dolphin's folders. Returns an error, or None."""
    p = props.prefs()
    props.load_paths_from_prefs()
    try:
        found = dolphin_config.find_running_dolphin(dme_names())
    except Exception as exc:  # never block Connect  # noqa: BLE001
        found = None
        print(f"[EBC] Dolphin folder detection failed: {exc}")
    if found is not None:
        props.remember_detected(found)
    if not runtime.dsu.start(int(p.dsu_port)):
        return runtime.dsu.error
    return None


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
        new_modes, isolating = ft.toggle_isolation(modes, self.slot)
        if isolating and ft.isolated_slot(modes) is None:
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
            if s.green_screen:
                props.update_green_screen(s)  # already on: sent again, now opaque
            else:
                s.green_screen = True  # its update sends it, opaque
            s.stage_display = disp.STAGE_HIDDEN
            s.hud = False
        elif s.isolate_saved:
            s.isolate_saved = False  # the swatch's own alpha again
            s.hud = s.isolate_hud
            s.stage_display = s.isolate_stage
            s.green_screen = s.isolate_green_screen
            props.update_green_screen(s)
        return {"FINISHED"}


class EBC_OT_clip_helper(Operator):
    """Create an empty that drives this clip plane: attached to the synced camera (Child Of, without its
    scale), on its view axis, so the clip value is minus its local Z (move it along Z only)"""

    bl_idname = "ebc.clip_helper"
    bl_label = "Add clip helper"
    bl_options = {"REGISTER", "UNDO"}

    which: EnumProperty(items=[("NEAR", "Near", ""), ("FAR", "Far", "")], default="FAR")
    stage_row: BoolProperty(
        name="For the stage row",
        description="Set it on the selected Stages row (used on that stage only) instead of the Camera panel",
        default=False,
    )

    @classmethod
    def description(cls, context, properties):
        doc = cls.__doc__ or ""
        return f"{doc}. Used on the selected stage only" if properties.stage_row else doc

    def execute(self, context):
        s = props.settings(context)
        row = None
        if self.stage_row:
            if not 0 <= s.stage_presets_index < len(s.stage_presets):
                self.report({"WARNING"}, "Select a row of the Stages list first")
                return {"CANCELLED"}
            row = s.stage_presets[s.stage_presets_index]
        camera = cameras.effective_camera(s, context.scene)
        if camera is None:
            self.report({"WARNING"}, "Pick or create the rig camera first")
            return {"CANCELLED"}
        near = self.which == "NEAR"
        name = "EBC_Near" if near else "EBC_Far"
        if row is not None:
            name = f"{name}_{row.name}"
        obj = cameras.new_empty(name, cameras.rig_collection(context), "SPHERE", 5.0)
        # Start where the plane is now, on the view axis: local Z = -clip value. A Child Of constraint
        # without scale instead of a parent: the helper follows the camera's location and rotation
        # but not its scale, so a camera scaled for the viewport never moves it (local unit = world unit).
        depth = float(s.near_clip if near else min(s.far_clip, 1000))
        con = obj.constraints.new("CHILD_OF")
        con.name = "EBC camera"
        con.target = camera
        con.use_scale_x = con.use_scale_y = con.use_scale_z = False
        con.set_inverse_pending = False
        con.inverse_matrix = Matrix.Identity(4)
        obj.location = (0.0, 0.0, -depth)
        obj.lock_location = (True, True, False)  # sideways does not change the depth: Z only
        target = row if row is not None else s
        if near:
            target.near_object = obj
        else:
            target.far_object = obj
        return {"FINISHED"}


class EBC_OT_create_rig(Operator):
    """Create the missing Origin, Camera and PLAYER_1..4 objects and assign them"""

    bl_idname = "ebc.create_rig"
    bl_label = "Create EBC rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        coll = cameras.rig_collection(context)
        created = []
        if s.origin is None:
            s.origin = bpy.data.objects.get("Origin") or cameras.new_empty("Origin", coll, "SPHERE", 5.0)
            created.append(s.origin.name)
        if s.camera is None:
            existing = bpy.data.objects.get("Camera")
            if existing is not None and existing.type == "CAMERA":
                s.camera = existing
            else:
                s.camera = cameras.new_camera(context, "Camera", s.origin)
                created.append(s.camera.name)
        for slot, attr in zip(props.SLOTS, props.PLAYER_PROPS, strict=True):
            if getattr(s, attr) is None:
                name = f"PLAYER_{slot + 1}"
                obj = bpy.data.objects.get(name) or cameras.new_empty(name, coll, "SINGLE_ARROW", 10.0)
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
        cam = cameras.effective_camera(props.settings(context), context.scene)
        return cam is not None and cam.type == "CAMERA"

    def execute(self, context):
        cam = cameras.effective_camera(props.settings(context), context.scene)
        cameras.set_default_fov(cam, context.scene)
        return {"FINISHED"}


AXONOMETRIC_PRESETS = {
    "ISOMETRIC": ("Isometric", cam_math.ELEVATION_ISOMETRIC),
    "DIMETRIC": ("Dimetric 2:1", cam_math.ELEVATION_DIMETRIC),
}


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
        return cameras.synced_camera(props.settings(context)) is not None

    def execute(self, context):
        s = props.settings(context)
        cam = cameras.synced_camera(s)
        if cam is None:
            self.report({"WARNING"}, "No synced camera")
            return {"CANCELLED"}
        label, elevation = AXONOMETRIC_PRESETS[self.preset]
        azimuth = float(s.axonometric_azimuth)
        target = cameras.aimed_point(cam)
        eye = cam.matrix_world.translation
        distance = max(1.0, sum((eye[i] - target[i]) ** 2 for i in range(3)) ** 0.5)
        if cam.data.type != "ORTHO":
            cameras.keep_framing_ortho(cam, context.scene, distance)  # the perspective it had, at its target
            cam.data.type = "ORTHO"
        cameras.set_pose_keep_scale(cam, Matrix(cam_math.axonometric_matrix(target, elevation, azimuth, distance)))
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
        cam = cameras.new_camera(context, f"EBC_Camera.{len(s.cameras) + 1:03d}", s.origin)
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
        util.remove_active(s, "cameras", "cameras_index")
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
        cameras.bind_marker(scene, frame, cam)
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
        aspect = cameras.game_aspect()
        r = context.scene.render
        r.resolution_x, r.pixel_aspect_x, r.pixel_aspect_y = cam_math.aspect_settings(aspect, r.resolution_y)
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
            cam = cameras.new_camera(context, record.GAME_CAMERA_NAME, None)
        else:
            return None
        s.record_camera = cam
    cameras.ensure_listed(s, cam)
    return cam


class EBC_OT_record_start(Operator):
    """Record the game camera into the chosen camera (keyframes at Blender frame = game frame)"""

    bl_idname = "ebc.record_start"
    bl_label = "Record"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected and not record.is_recording() and not screenshot.busy()

    def execute(self, context):
        s = props.settings(context)
        if s.record_mode == record.RecordMode.STEP:
            runtime.devserver.set_port(s.devserver_port)
            if runtime.devserver.status() is None and not runtime.dsu.connected:
                self.report({"WARNING"}, record.STEP_UNAVAILABLE)
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
    coll = cameras.rig_collection(context)
    data = bpy.data.cameras.new(BLEND_NAME)
    for attr in ("sensor_width", "sensor_height", "sensor_fit", "clip_start", "clip_end", "shift_x", "shift_y"):
        setattr(data, attr, getattr(src.data, attr))
    ortho_a, ortho_b = cameras.is_ortho(src), cameras.is_ortho(dst)
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
    cameras.bind_marker(scene, start, blend)
    cameras.bind_marker(scene, end, dst)
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
        if cameras.is_ortho(src) != cameras.is_ortho(dst):
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
    except Exception:  # not installed as an extension (dev checkout)  # noqa: BLE001
        base = os.path.join(tempfile.gettempdir(), "ebc_states")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"slot{slot}.sav")


CONTROL_ACTIONS = [
    ("CHECK", "Check", "Check which emulator control answers (DevServer, Dolphin hotkeys)"),
    ("TOGGLE", "Pause / Resume", "Pause the emulation when it runs, resume it when it is paused"),
    ("PAUSE", "Pause", "Pause the emulation"),
    ("RESUME", "Resume", "Resume the emulation"),
    (
        "STEP",
        "Next frame",
        "Advance one frame (Game pause and Dolphin hotkeys: exactly one; DevServer: approximate). When it runs, pause",
    ),
    ("SAVE", "Save state", "Save a state in the selected slot"),
    ("LOAD", "Load state", "Load the state of the selected slot"),
]


GAME_PAUSE_ACTIONS = frozenset({"TOGGLE", "PAUSE", "RESUME", "STEP"})  # what Game pause (debug mode) takes over


class EBC_OT_devserver(Operator):
    """Control the emulator: through the Dolphin DevServer when it answers, else through Dolphin's own
    hotkeys (EBC's DSU pad)"""

    bl_idname = "ebc.devserver"
    bl_label = "Emulator control"

    action: EnumProperty(items=CONTROL_ACTIONS, default="CHECK")

    @classmethod
    def description(cls, context, properties):
        return {ident: desc for ident, _name, desc in CONTROL_ACTIONS}.get(properties.action, cls.__doc__)

    def execute(self, context):
        s = props.settings(context)
        if self.action != "CHECK" and record.drives_emulator():
            # the recorder drives the same pad / DevServer frame by frame: never two drivers at once
            self.report({"WARNING"}, "Stop the frame-by-frame recording first: it drives the emulator")
            return {"CANCELLED"}
        if s.game_pause and self.action in GAME_PAUSE_ACTIONS:  # memory writes: no DevServer / hotkeys
            return self._game_pause()
        client = runtime.devserver
        client.set_port(s.devserver_port)
        if self.action == "CHECK" or not client.available:
            status = client.status()  # the DevServer may have appeared (or gone)
            runtime.note_running(None if status is None else status.running)
        if self.action == "CHECK":
            kind = runtime.control_kind()
            self.report({"INFO"}, f"Emulator control: {emucontrol.LABELS[kind]}")
            return {"FINISHED"} if kind != emucontrol.NONE else {"CANCELLED"}
        if self.action in {"SAVE", "LOAD"}:
            runtime.get_game().before_savestate()
        ctl = runtime.control()
        if ctl is None:
            self.report(
                {"WARNING"},
                "No emulator control: DevServer unreachable and Dolphin not configured for EBC "
                f"({runtime.dsu.error or 'Configure Dolphin for EBC, then restart Dolphin'})",
            )
            return {"CANCELLED"}
        ok = self._run(ctl, s)
        if not ok and ctl is client and runtime.dsu.connected:  # the DevServer went away: hotkeys
            ctl = runtime.dsu_control
            ok = self._run(ctl, s)
        if not ok:
            name = "DevServer" if ctl is client else "Dolphin hotkeys"
            self.report({"WARNING"}, f"{name}: {ctl.last_error or 'unreachable'}")
            return {"CANCELLED"}
        if ctl is not client and ctl.last_error:
            self.report({"INFO"}, ctl.last_error)
        return {"FINISHED"}

    def _game_pause(self):
        """Play / Pause and Next frame through P+'s debug pause (core/debugmode.py): memory writes only."""
        debug = runtime.get_game().debug
        run = {"TOGGLE": debug.toggle, "PAUSE": debug.pause, "RESUME": debug.resume, "STEP": debug.step}
        if not run[self.action]():
            self.report({"WARNING"}, f"Game pause: {debug.last_error or 'failed'}")
            return {"CANCELLED"}
        return {"FINISHED"}

    def _run(self, ctl, s) -> bool:
        game = runtime.get_game()
        action = self.action
        if action == "TOGGLE":
            running = _is_running(ctl)
            action = "PAUSE" if running or (running is None and runtime.emulation_running is not False) else "RESUME"
        if action == "PAUSE":
            ok = ctl.pause()
            runtime.note_running(False if ok else None)
            return ok
        if action == "RESUME":
            ok = ctl.resume()
            runtime.note_running(True if ok else None)
            return ok
        if action == "STEP":
            ok = ctl.advance_frame(game.current_frame)
            runtime.note_running(False if ok else None)  # a Frame Advance leaves the game paused
            return ok
        if self.action == "SAVE":
            return game.save_state(lambda: ctl.save_state(state_path(s.state_slot)))
        ok = ctl.load_state(state_path(s.state_slot))
        if ok:
            if ctl is runtime.devserver:
                game.after_state_load()
            else:  # Dolphin loads the slot on its own thread, a little later
                for delay in (0.3, 1.0):
                    bpy.app.timers.register(_reapply_later, first_interval=delay)
        return ok


def _is_running(ctl) -> bool | None:
    """Ask the emulator now (an action, not a redraw): the DevServer status, else the frame counters."""
    if ctl is runtime.devserver:
        status = ctl.status()
        return None if status is None else status.running
    return ctl.is_running()


def _reapply_later():
    try:
        runtime.get_game().after_state_load()
    except Exception as exc:  # never raise from a timer  # noqa: BLE001
        print(f"[EBC] re-apply after state load failed: {exc}")
    return None


class EBC_OT_screenshot(Operator):
    """Take a full-quality screenshot with Dolphin (its own capture, at the internal resolution), named
    <stage>_<game frame>_<date-time>.png (e.g. Battlefield_L_600_20261011-132255.png; EBC_<scene>_... when
    no stage is identified), in the Screenshot folder (Options; by default Dolphin's own). When the game
    is paused, it advances 4 frames to take it (with your camera)"""

    bl_idname = "ebc.screenshot"
    bl_label = "Screenshot"

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected and not screenshot.busy() and not record.drives_emulator()

    def execute(self, context):
        err = screenshot.start(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class EBC_OT_configure_dolphin(Operator):
    """Close Dolphin first. Writes once in Dolphin's configuration: EBC's DSU pad as an input source, the
    Screenshot, Toggle Pause, Frame Advance and Save/Load State Slot 10 hotkeys bound to it (your keys are
    kept), hotkeys active without window focus. Backups: <file>.ebc-backup"""

    bl_idname = "ebc.configure_dolphin"
    bl_label = "Configure Dolphin for EBC"

    folder: StringProperty(name="Folder", description="Dolphin folder to configure (empty: preferences / detected)")

    def execute(self, context):
        p = props.prefs()
        props.load_paths_from_prefs()
        if self.folder:
            paths, how = dolphin_config.locate(self.folder)
        else:
            paths, how = dolphin_config.locate(p.dolphin_folder, runtime.dolphin_paths)
        if paths is None:
            self.report({"ERROR"}, how)
            return {"CANCELLED"}
        try:
            running = dolphin_config.dolphin_uses(paths.config, names=dme_names())
            result = dolphin_config.install(paths.config, int(p.dsu_port), running=running)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not write Dolphin's configuration: {exc}")
            return {"CANCELLED"}
        if not result.ok:
            self.report({"ERROR"}, result.message)
            return {"CANCELLED"}
        if self.folder:
            props.remember_detected(paths)
        lines = [result.message + f" ({how})"]
        if result.changed:
            lines.append(f"Changed: {', '.join(result.changed)}; backups: {', '.join(result.backups) or 'kept'}")
        lines += result.notes
        lines.append(
            "Dolphin's hotkeys now work without its window focused: your keyboard keys (F9, F10, F1-F8...) "
            "also act on Dolphin while you type in Blender"
        )
        lines.append("Start Dolphin, then Connect: the Emulator control panel shows 'Dolphin hotkeys (DSU)'")
        for line in lines:
            self.report({"INFO"}, line)
        return {"FINISHED"}


class EBC_OT_notify(Operator):
    """Show a message in the status bar (used by background tasks)"""

    bl_idname = "ebc.notify"
    bl_label = "EBC message"
    bl_options = {"INTERNAL"}

    level: StringProperty(default="INFO")
    message: StringProperty(default="")

    def execute(self, context):
        self.report({self.level if self.level in {"INFO", "WARNING", "ERROR"} else "INFO"}, self.message)
        return {"FINISHED"}


classes = (
    EBC_OT_connect,
    EBC_OT_clip_helper,
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
    EBC_OT_screenshot,
    EBC_OT_configure_dolphin,
    EBC_OT_notify,
)
