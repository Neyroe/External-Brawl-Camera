"""Sidebar panels (View3D > N panel > "EBC").

No ``poll`` on ``context.object``: the panels are always there. Panels that
talk to the game are greyed out while Dolphin is not hooked; Rig and Cameras
only touch the scene and stay usable offline. No I/O
in ``draw()``: the connection state is a local flag, the DevServer state is
the last known one.
"""

from bpy.types import Panel, UIList

from .. import runtime
from ..core import game as g
from . import props, record, sync

CATEGORY = "EBC"
# Relative render/game aspect difference shown as a warning (0.1 % = about 1 px across 834).
ASPECT_TOLERANCE = 0.001


class _EBCPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY

    @staticmethod
    def connected() -> bool:
        return runtime.get_game().connected

    def gate(self) -> bool:
        connected = self.connected()
        self.layout.enabled = connected
        return connected


class EBC_PT_connection(_EBCPanel, Panel):
    bl_idname = "EBC_PT_connection"
    bl_label = "Connection"

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        game = runtime.get_game()

        row = layout.row(align=True)
        if game.connected:
            row.label(text="Dolphin: connected", icon="CHECKMARK")
            row.operator("ebc.disconnect", text="", icon="X")
        else:
            row.label(text="Dolphin: not connected", icon="ERROR")
            row.operator("ebc.connect", text="Connect", icon="LINKED")

        row = layout.row(align=True)
        if game.connected:
            unsure = not game.profile_sure
            row.label(text=f"Game: {game.profile_info or game.p.label}", icon="ERROR" if unsure else "BLANK1")
        row.prop(s, "game_version", text="")

        row = layout.row(align=True)
        dev = runtime.devserver
        row.label(
            text="DevServer: available" if dev.available else "DevServer: unavailable (optional)",
            icon="CHECKMARK" if dev.available else "BLANK1",
        )
        row.prop(s, "devserver_port", text="")
        row.operator("ebc.devserver", text="", icon="FILE_REFRESH").action = "CHECK"

        col = layout.column(align=True)
        col.enabled = game.connected
        info = col.row()
        info.label(text=f"Stage: {s.stage_id}")
        info.label(text=f"Frame: {s.frame_number}")

        row = col.row()
        row.scale_y = 1.6
        if sync.is_running():
            row.operator("ebc.sync_stop", text="Stop sync", icon="PAUSE")
        else:
            row.operator("ebc.sync_start", text="Start sync", icon="PLAY")
        col.operator("ebc.reapply", icon="FILE_REFRESH")
        io = sync.io_summary() if sync.is_running() else None
        if io:
            sub = col.row()
            sub.enabled = False  # discreet: memory access budget of the last tick
            sub.label(text=io)
        err = sync.last_error()
        if err and not sync.is_running():
            col.label(text=err, icon="INFO")


class EBC_PT_rig(_EBCPanel, Panel):
    bl_idname = "EBC_PT_rig"
    bl_label = "Rig"

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        col = layout.column(align=True)
        col.prop(s, "camera")
        col.prop(s, "origin")
        col = layout.column(align=True)
        for slot in props.SLOTS:
            col.prop(s, f"player_{slot + 1}")
        layout.operator("ebc.create_rig", icon="OUTLINER_OB_EMPTY")


class EBC_PT_camera(_EBCPanel, Panel):
    bl_idname = "EBC_PT_camera"
    bl_label = "Camera"

    # Only the controls that write to the game at once are greyed out offline;
    # the others are settings used by the sync, and Reset FOV only touches Blender.

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        connected = self.connected()
        row = layout.row()
        row.enabled = connected
        row.prop(s, "camera_direction", expand=True)
        # Same camera as the one the sync sends: the scene camera if listed, else the Rig one.
        cam = sync.effective_camera(s, context.scene)
        ortho = sync.is_ortho(cam)
        row = layout.row(align=True)
        row.label(text="Projection:")
        row.prop(s, "projection", expand=True)
        row = layout.row(align=True)
        row.operator("ebc.camera_axonometric", text="Isometric").preset = "ISOMETRIC"
        row.operator("ebc.camera_axonometric", text="Dimetric 2:1").preset = "DIMETRIC"
        row.prop(s, "axonometric_azimuth", text="")
        if (ortho or runtime.get_game().ortho_active) and s.stage_id in runtime.get_game().p.ORTHO_DECAL_STAGES:
            layout.label(text="Some floor decals are not drawn in orthographic on this stage", icon="ERROR")
        row = layout.row(align=True)
        if ortho:
            row.label(text="Ortho scale")
            row.prop(cam.data, "ortho_scale", text="")
        else:
            row.prop(s, "sync_fov")
            if cam is not None and cam.type == "CAMERA":
                row.prop(cam.data, "lens", text="")
            row.operator("ebc.reset_fov", text="", icon="FILE_REFRESH")
        aspect = runtime.get_game().aspect or g.GAME_DEFAULT_ASPECT
        r = context.scene.render
        if r.resolution_y:
            blender_aspect = r.resolution_x * r.pixel_aspect_x / (r.resolution_y * r.pixel_aspect_y)
            if abs(blender_aspect - aspect) > ASPECT_TOLERANCE * aspect:
                row = layout.row(align=True)
                row.label(text=f"Render aspect {blender_aspect:.4f} != game {aspect:.4f}", icon="ERROR")
                row.operator("ebc.match_game_aspect", text="Match game aspect")
        warning = sync.camera_warning() if sync.is_running() else None
        if warning:
            layout.label(text=warning, icon="ERROR")
        col = layout.column(align=True)
        col.enabled = connected and not ortho
        col.prop(s, "near_clip", slider=True)
        col.prop(s, "far_clip", slider=True, text="Far clip (max = ∞)")
        if ortho:
            col.label(text="Orthographic: near/far set by EBC", icon="INFO")
        col = layout.column()
        col.prop(s, "keep_origin_on_plane")
        col.prop(s, "copy_camera_roll")
        col.prop(s, "disable_screen_shake")
        col.prop(s, "show_player_positions")
        layout.label(text="Pause camera: experimental", icon="EXPERIMENTAL")


class EBC_PT_timeline(_EBCPanel, Panel):
    bl_idname = "EBC_PT_timeline"
    bl_label = "Timeline"

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        layout.prop(s, "timeline_mode")
        if s.timeline_mode == "START_AT":
            layout.prop(s, "start_frame")


class EBC_PT_emulator(_EBCPanel, Panel):
    bl_idname = "EBC_PT_emulator"
    bl_label = "Emulator control"
    bl_parent_id = "EBC_PT_timeline"

    def draw(self, context):
        layout = self.layout
        layout.enabled = self.connected() and runtime.devserver.available
        s = props.settings(context)
        if not runtime.devserver.available:
            layout.label(text="DevServer unreachable", icon="INFO")
        row = layout.row(align=True)
        row.operator("ebc.devserver", text="Load state", icon="IMPORT").action = "LOAD"
        row.prop(s, "state_slot", expand=True)
        row.operator("ebc.devserver", text="Save state", icon="EXPORT").action = "SAVE"
        row = layout.row(align=True)
        row.operator("ebc.devserver", text="Pause", icon="PAUSE").action = "PAUSE"
        row.operator("ebc.devserver", text="Next frame", icon="FRAME_NEXT").action = "STEP"
        row.operator("ebc.devserver", text="Resume", icon="PLAY").action = "RESUME"


class EBC_PT_green_screen(_EBCPanel, Panel):
    bl_idname = "EBC_PT_green_screen"
    bl_label = "Green screen"

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        row = layout.row()
        row.prop(s, "green_screen")
        row.prop(s, "green_screen_color", text="")
        row = layout.row()
        row.prop(s, "stage_void")
        row.prop(s, "stage_void_color", text="")
        layout.prop(s, "stage_display")
        if s.isolate_saved:
            layout.label(text="Isolated: background opaque, stage hidden", icon="SOLO_ON")
        col = layout.column(align=True)
        col.label(text="For a pure key colour: Full-screen background", icon="INFO")
        col.label(text="at alpha 255, or Stage: Hidden + Void colour", icon="BLANK1")


class EBC_PT_fighter_tint(_EBCPanel, Panel):
    bl_idname = "EBC_PT_fighter_tint"
    bl_label = "Per-character colour [experimental]"
    bl_parent_id = "EBC_PT_green_screen"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        isolated = g.isolated_slot(s.fighter_modes())
        for slot in props.SLOTS:
            tint = s.tint(slot)
            split = layout.split(factor=0.12, align=True)
            split.label(text=f"P{slot + 1}")
            row = split.row(align=True)
            row.prop(tint, "mode", text="")
            sub = row.row(align=True)
            sub.active = tint.mode == g.FIGHTER_COLOUR
            sub.prop(tint, "color", text="")
            sub.prop(tint, "alpha", text="Alpha")
            icon = "SOLO_ON" if isolated == slot else "SOLO_OFF"
            row.operator("ebc.isolate_fighter", text="", icon=icon).slot = slot


class EBC_PT_display(_EBCPanel, Panel):
    bl_idname = "EBC_PT_display"
    bl_label = "Display"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        grid = layout.grid_flow(columns=2, even_columns=True)
        grid.prop(s, "hud")
        grid.prop(s, "debug_menu")
        grid.prop(s, "draw_di")
        grid.prop(s, "lock_game_camera")
        layout.prop(s, "freeze_method")
        layout.prop(s, "characters_display")


class EBC_PT_shadows(_EBCPanel, Panel):
    bl_idname = "EBC_PT_shadows"
    bl_label = "Shadows"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        col = layout.column(align=True)
        col.prop(s, "light_angle_x", slider=True)
        col.prop(s, "light_angle_y", slider=True)
        layout.label(text="Sent once changed; re-applied on each match", icon="INFO")


class EBC_PT_audio(_EBCPanel, Panel):
    bl_idname = "EBC_PT_audio"
    bl_label = "Audio"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        col = layout.column(align=True)
        col.prop(s, "music_volume", slider=True)
        col.prop(s, "sfx_volume", slider=True)


class EBC_UL_cameras(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        cam = item.camera
        scene = context.scene
        row = layout.row(align=True)
        if cam is None:
            row.prop(item, "camera", text="", emboss=False)
            return
        frames = [m.frame for m in scene.timeline_markers if m.camera == cam]
        is_active = scene.camera == cam
        row.prop(item, "camera", text="", emboss=False, icon="VIEW_CAMERA" if is_active else "CAMERA_DATA")
        if frames:
            row.label(text=", ".join(str(f) for f in sorted(frames)[:4]) + ("…" if len(frames) > 4 else ""))
        op = row.operator(
            "ebc.camera_activate", text="", icon="RADIOBUT_ON" if is_active else "RADIOBUT_OFF", depress=is_active
        )
        op.index = index


class EBC_PT_cameras(_EBCPanel, Panel):
    bl_idname = "EBC_PT_cameras"
    bl_label = "Cameras"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        main = s.camera
        on_main = main is not None and context.scene.camera == main
        row = layout.row(align=True)
        row.label(text=f"Rig camera: {main.name}" if main else "Rig camera: none", icon="OUTLINER_OB_CAMERA")
        op = row.operator(
            "ebc.camera_activate", text="", icon="RADIOBUT_ON" if on_main else "RADIOBUT_OFF", depress=on_main
        )
        op.index = -1
        row = layout.row()
        row.template_list("EBC_UL_cameras", "", s, "cameras", s, "cameras_index", rows=3)
        col = row.column(align=True)
        col.operator("ebc.camera_add", text="", icon="ADD")
        col.operator("ebc.camera_remove", text="", icon="REMOVE")
        layout.operator("ebc.camera_bind_marker", icon="MARKER_HLT")
        layout.label(text="Click a dot to cut to that camera. Markers switch it on playback.", icon="INFO")


class EBC_PT_record(_EBCPanel, Panel):
    bl_idname = "EBC_PT_record"
    bl_label = "Record game camera"
    bl_parent_id = "EBC_PT_cameras"

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        recording = record.is_recording()
        col = layout.column()
        col.enabled = not recording
        col.prop(s, "record_camera", text="Into")
        if s.record_camera is None:
            col.label(text=f"Empty: creates {record.GAME_CAMERA_NAME}", icon="INFO")
        col.row().prop(s, "record_mode", expand=True)
        col.prop(s, "record_include_shake")
        col.prop(s, "record_end_frame")
        if s.record_mode == "STEP" and not runtime.devserver.available:
            col.label(text="Frame by frame needs the DevServer", icon="INFO")
        row = layout.row()
        row.scale_y = 1.4
        row.enabled = self.connected() or recording
        if recording:
            row.operator("ebc.record_stop", text="Stop", icon="SNAP_FACE")
        else:
            row.operator("ebc.record_start", text="Record", icon="REC")
        st = record.stats()
        if st is not None:
            state = "Recording" if recording else "Recorded"
            span = f" ({st.first}-{st.last})" if st.first is not None else ""
            layout.label(text=f"{state}: {st.captured} frames{span}", icon="KEYFRAME")
            if st.missed or st.repeated:
                layout.label(text=f"{st.missed} missed, {st.repeated} repeated", icon="ERROR")
        err = record.last_error()
        if err:
            layout.label(text=err, icon="ERROR")


class EBC_PT_transitions(_EBCPanel, Panel):
    bl_idname = "EBC_PT_transitions"
    bl_label = "Transitions"
    bl_parent_id = "EBC_PT_cameras"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        col = layout.column(align=True)
        col.prop(s, "transition_frames")
        col.prop(s, "transition_easing")
        col.prop(s, "transition_reverse")
        layout.operator("ebc.transition_add", icon="IPO_EASE_IN_OUT")
        layout.label(text="Game camera ↔ selected camera, from this frame", icon="INFO")
        layout.separator()
        row = layout.row(align=True)
        row.prop(s, "hand_back_to_game")
        row.operator("ebc.hand_back", text="", icon="KEYFRAME_HLT")
        layout.label(text="Releases the lock: the game eases back by itself", icon="INFO")


classes = (
    EBC_PT_connection,
    EBC_PT_rig,
    EBC_PT_camera,
    EBC_PT_timeline,
    EBC_PT_emulator,
    EBC_PT_green_screen,
    EBC_PT_fighter_tint,
    EBC_PT_display,
    EBC_PT_shadows,
    EBC_PT_audio,
    EBC_UL_cameras,
    EBC_PT_cameras,
    EBC_PT_record,
    EBC_PT_transitions,
)
