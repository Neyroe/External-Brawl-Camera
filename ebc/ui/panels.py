"""Sidebar panels (View3D > N panel > "EBC").

No ``poll`` on ``context.object``: the panels are always there. Panels that
talk to the game are greyed out while Dolphin is not hooked; Rig and Cameras
only touch the scene and stay usable offline. No I/O in ``draw()``: the
connection state is a local flag, the DevServer, DSU and Play / Pause states
are the last known ones.

Layout rule: what a session uses all the time is in the main panels; settings
used now and then are in "Options" / "Advanced" sub-panels, closed by default.
Information lines show only when they apply.
"""

from bpy.types import Panel, UIList

from .. import runtime
from ..core import emucontrol
from ..core import fighters as ft
from ..core import freeze as fz
from . import cameras, props, record, screenshot, stage_presets, stages, sync

CATEGORY = "EBC"
# Relative render/game aspect difference shown as a warning (0.1 % = about 1 px across 834).
ASPECT_TOLERANCE = 0.001
CONFIGURE_TEXT = "Configure Dolphin for EBC"


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


# -- Connection -------------------------------------------------------------------------------------


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
            unsure = not game.profile_sure
            layout.label(text=f"Game: {game.profile_info or game.p.label}", icon="ERROR" if unsure else "BLANK1")
        else:
            row.label(text="Dolphin: not connected", icon="ERROR")
            row.operator("ebc.connect", text="Connect", icon="LINKED")

        col = layout.column(align=True)
        col.enabled = game.connected
        col.label(text=f"Stage: {stage_presets.stage_label(s)}")
        col.label(text=f"Frame: {s.frame_number}    Stage id: {s.stage_id}")
        row = col.row()
        row.scale_y = 1.6
        if sync.is_running():
            row.operator("ebc.sync_stop", text="Stop sync", icon="PAUSE")
        else:
            row.operator("ebc.sync_start", text="Start sync", icon="PLAY")
        col.operator("ebc.reapply", icon="FILE_REFRESH")
        err = sync.last_error()
        if err and not sync.is_running():
            col.label(text=err, icon="INFO")


class EBC_PT_connection_options(_EBCPanel, Panel):
    bl_idname = "EBC_PT_connection_options"
    bl_label = "Options"
    bl_parent_id = "EBC_PT_connection"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        layout.prop(s, "game_version")
        row = layout.row(align=True)
        dev = runtime.devserver
        row.label(text="DevServer: available" if dev.available else "DevServer: off (optional)")
        row.prop(s, "devserver_port", text="")
        row.operator("ebc.devserver", text="", icon="FILE_REFRESH").action = "CHECK"
        io = sync.io_summary() if sync.is_running() else None
        if io:
            sub = layout.row()
            sub.enabled = False  # discreet: memory access budget of the last tick
            sub.label(text=io)


# -- Rig --------------------------------------------------------------------------------------------


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
        for attr in props.PLAYER_PROPS:
            col.prop(s, attr)
        layout.operator("ebc.create_rig", icon="OUTLINER_OB_EMPTY")


# -- Camera -----------------------------------------------------------------------------------------


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
        cam = cameras.effective_camera(s, context.scene)
        ortho = cameras.is_ortho(cam)
        draw_lens(layout, s, cam, ortho)
        draw_camera_warnings(layout, context, s, ortho)
        draw_clip(layout, s, cam, connected, ortho)
        grid = layout.grid_flow(columns=2, even_columns=True, align=True)
        grid.prop(s, "copy_camera_roll", text="Copy roll")
        grid.prop(s, "disable_screen_shake", text="No screen shake")
        grid.prop(s, "show_player_positions", text="Player positions")
        grid.prop(s, "skip_stage_intro", text="Skip stage intro")
        draw_intro_route(layout, s)


def draw_lens(layout, s, cam, ortho: bool) -> None:
    row = layout.row(align=True)
    if ortho:
        row.label(text="Ortho scale")
        row.prop(cam.data, "ortho_scale", text="")
        return
    row.prop(s, "sync_fov")
    if cam is not None and cam.type == "CAMERA":
        row.prop(cam.data, "lens", text="")
    row.operator("ebc.reset_fov", text="", icon="FILE_REFRESH")


def draw_camera_warnings(layout, context, s, ortho: bool) -> None:
    """Only what applies now: render aspect, unrepresentable aim, decals in orthographic."""
    game = runtime.get_game()
    aspect = cameras.game_aspect()
    if context.scene.render.resolution_y:
        blender_aspect = cameras.render_aspect(context.scene)
        if abs(blender_aspect - aspect) > ASPECT_TOLERANCE * aspect:
            row = layout.row(align=True)
            row.label(text=f"Render aspect {blender_aspect:.4f} ≠ game {aspect:.4f}", icon="ERROR")
            row.operator("ebc.match_game_aspect", text="Match")
    warning = sync.camera_warning() if sync.is_running() else None
    if warning:
        layout.label(text=warning, icon="ERROR")
    if (ortho or game.ortho_active) and s.stage_id in game.p.ORTHO_DECAL_STAGES:
        layout.label(text="Some floor decals are not drawn in orthographic on this stage", icon="ERROR")


def draw_clip(layout, s, cam, connected: bool, ortho: bool) -> None:
    """Slider, object and + per plane. With an object (the Camera panel's, or the current stage
    row's), the value it gives now replaces the slider: depth in front of the camera, so a helper
    made by + reads minus its local Z."""
    col = layout.column(align=True)
    col.enabled = connected and not ortho
    stage = stage_presets.effective_overrides(s)
    for prop, obj_prop, which, text in (
        ("near_clip", "near_object", "NEAR", "Near clip"),
        ("far_clip", "far_object", "FAR", "Far clip (max = ∞)"),
    ):
        obj = getattr(stage, obj_prop)
        row = col.row(align=True)
        depth = sync.clip_depth(cam, obj)
        if obj is None or depth is None:
            row.prop(s, prop, slider=True, text=text)
        else:
            value = sync.clip_value(prop, depth)
            shown = "∞" if prop == "far_clip" and value >= sync.CLIP_RANGES[prop][1] else str(value)
            row.label(text=f"{text.split(' (')[0]}: {shown}")
        if obj is not None and obj != getattr(s, obj_prop):
            row.label(text=f"{obj.name} (stage)", icon="WORLD")  # the stage row's object wins
            continue
        row.prop(s, obj_prop, text="", icon="OBJECT_DATA")
        if obj is None:
            row.operator("ebc.clip_helper", text="", icon="ADD").which = which
    if ortho:
        col.label(text="Orthographic: near/far set by EBC", icon="INFO")


def draw_intro_route(layout, s) -> None:
    """The Skip stage intro camera route in use, when the option is on (no I/O)."""
    if not s.skip_stage_intro:
        return
    game = runtime.get_game()
    col = layout.column(align=True)
    sub = col.column(align=True)
    sub.enabled = False
    if not game.connected:
        sub.label(text="Intro skip: chosen on Connect (DevServer patch, else data)", icon="BLANK1")
        return
    sub.label(text=f"Intro skip: {game.intro.label}", icon="BLANK1")
    if game.intro.error:
        col.label(text=game.intro.error, icon="ERROR")


class EBC_PT_camera_advanced(_EBCPanel, Panel):
    bl_idname = "EBC_PT_camera_advanced"
    bl_label = "Advanced"
    bl_parent_id = "EBC_PT_camera"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        row = layout.row(align=True)
        row.label(text="Projection")
        row.prop(s, "projection", expand=True)
        row = layout.row(align=True)
        row.operator("ebc.camera_axonometric", text="Isometric").preset = "ISOMETRIC"
        row.operator("ebc.camera_axonometric", text="Dimetric 2:1").preset = "DIMETRIC"
        row.prop(s, "axonometric_azimuth", text="")
        if s.freeze_method == fz.FREEZE_CODE_MENU:
            # Code Menu lock only: the game keeps the aim on the stage plane, the Origin can follow it
            layout.prop(s, "keep_origin_on_plane")
        if not s.lock_game_camera:  # frozen, the pause camera is driven exactly (pause-camera.md)
            layout.label(text="Pause camera without Freeze: experimental", icon="EXPERIMENTAL")


# -- Timeline and emulator control ------------------------------------------------------------------


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
        draw_pause_at(layout, s)


def draw_pause_at(layout, s) -> None:
    """Pause at frame (0 = off), the stage row's value when it sets one, and what the last one did."""
    row = layout.row(align=True)
    row.prop(s, "pause_frame")
    stage_row = stage_presets.current_row(s)
    if stage_row is not None and stage_row.pause_frame > 0:
        row.label(text=f"{stage_row.name}: {stage_row.pause_frame}", icon="WORLD")
    msg = sync.pause_message()
    if msg:
        layout.label(text=msg, icon="PAUSE")


def draw_control_state(layout) -> None:
    """Which emulator control answers, and whether Dolphin listens to EBC's DSU pad (no I/O)."""
    kind = runtime.control_kind()
    icon = "CHECKMARK" if kind != emucontrol.NONE else "BLANK1"
    layout.label(text=f"Emulator control: {emucontrol.LABELS[kind]}", icon=icon)
    if runtime.dsu.connected:
        layout.label(text="Dolphin hotkeys: configured", icon="CHECKMARK")
        return
    # The Screenshot always needs the DSU pad (the DevServer has no capture): offered whatever answers.
    layout.label(text=runtime.dsu.error or "Dolphin hotkeys: not configured", icon="ERROR")
    draw_configure(layout)


def draw_configure(layout) -> None:
    row = layout.row()
    row.operator("ebc.configure_dolphin", text=CONFIGURE_TEXT, icon="PREFERENCES")


def play_pause(game_pause: bool = False) -> tuple[str, str]:
    """(label, icon) of the Play / Pause button from the last known state (no I/O): the game's debug
    pause when ``game_pause``, else the emulation."""
    paused = runtime.get_game().debug.known_paused is True if game_pause else runtime.emulation_running is False
    if paused:
        return "Resume", "PLAY"
    return "Pause", "PAUSE"


class EBC_PT_emulator(_EBCPanel, Panel):
    bl_idname = "EBC_PT_emulator"
    bl_label = "Emulator control"
    bl_parent_id = "EBC_PT_timeline"

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        connected = self.connected()
        kind = runtime.control_kind()
        draw_control_state(layout)
        recording = record.drives_emulator()  # frame by frame, the recorder drives: no second driver
        controls = connected and kind != emucontrol.NONE and not recording
        col = layout.column(align=True)
        row = col.row(align=True)
        row.enabled = controls
        row.operator("ebc.devserver", text="Load", icon="IMPORT").action = "LOAD"
        row.prop(s, "state_slot", expand=True)
        row.operator("ebc.devserver", text="Save", icon="EXPORT").action = "SAVE"
        row = col.row(align=True)
        left = row.row(align=True)
        # the game pause is plain memory writes: no DevServer nor hotkeys needed
        left.enabled = (connected and not recording) if s.game_pause else controls
        label, icon = play_pause(s.game_pause)
        left.operator("ebc.devserver", text=label, icon=icon).action = "TOGGLE"
        left.operator("ebc.devserver", text="Next frame", icon="FRAME_NEXT").action = "STEP"
        right = row.row(align=True)
        right.enabled = connected and not recording  # Dolphin's own capture, through the DSU pad
        right.operator("ebc.screenshot", text="Screenshot", icon="RESTRICT_RENDER_OFF")
        col.prop(s, "game_pause")
        msg = screenshot.last_message()
        if msg:
            failed = msg.startswith("Screenshot failed")
            layout.label(text=msg, icon="ERROR" if failed else "IMAGE_DATA")
            if failed and not runtime.dsu.connected:
                draw_configure(layout)


class EBC_PT_emulator_options(_EBCPanel, Panel):
    bl_idname = "EBC_PT_emulator_options"
    bl_label = "Options"
    bl_parent_id = "EBC_PT_emulator"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = props.settings(context)
        self.layout.prop(s, "screenshot_folder")
        if not s.screenshot_folder.strip():  # empty: say which folder that is
            folder = screenshot.dolphin_folder()
            self.layout.label(text=f"Dolphin's: {folder}" if folder else "Dolphin's folder (unknown until Connect)")


# -- Green screen -----------------------------------------------------------------------------------


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
        elif s.green_screen and s.green_screen_color[3] < 1.0:
            layout.label(text="Pure key colour: alpha 1, or Stage Hidden + Void colour", icon="INFO")


class EBC_PT_fighter_tint(_EBCPanel, Panel):
    bl_idname = "EBC_PT_fighter_tint"
    bl_label = "Per-character colour [experimental]"
    bl_parent_id = "EBC_PT_green_screen"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        isolated = ft.isolated_slot(s.fighter_modes())
        for slot in props.SLOTS:
            tint = s.tint(slot)
            split = layout.split(factor=0.12, align=True)
            split.label(text=f"P{slot + 1}")
            row = split.row(align=True)
            row.prop(tint, "mode", text="")
            sub = row.row(align=True)
            sub.active = tint.mode == ft.FIGHTER_COLOUR
            sub.prop(tint, "color", text="")
            sub.prop(tint, "alpha", text="Alpha")
            icon = "SOLO_ON" if isolated == slot else "SOLO_OFF"
            row.operator("ebc.isolate_fighter", text="", icon=icon).slot = slot


# -- Display, shadows, audio ------------------------------------------------------------------------


class EBC_PT_display(_EBCPanel, Panel):
    bl_idname = "EBC_PT_display"
    bl_label = "Display"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        layout = self.layout
        s = props.settings(context)
        grid = layout.grid_flow(columns=2, even_columns=True, align=True)
        grid.prop(s, "hud")
        grid.prop(s, "debug_menu")  # Code Menu Debug Mode (also set by Emulator control > Game pause)
        grid.prop(s, "draw_di")
        grid.prop(s, "lock_game_camera", text="Lock camera")
        layout.prop(s, "freeze_method")
        layout.prop(s, "characters_display")


class EBC_PT_shadows(_EBCPanel, Panel):
    bl_idname = "EBC_PT_shadows"
    bl_label = "Shadows"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        s = props.settings(context)
        col = self.layout.column(align=True)
        col.prop(s, "light_angle_x", slider=True)
        col.prop(s, "light_angle_y", slider=True)


class EBC_PT_audio(_EBCPanel, Panel):
    bl_idname = "EBC_PT_audio"
    bl_label = "Audio"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.gate()
        s = props.settings(context)
        col = self.layout.column(align=True)
        col.prop(s, "music_volume", slider=True)
        col.prop(s, "sfx_volume", slider=True)


# -- Cameras, record, transitions -------------------------------------------------------------------


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
        row = col.row(align=True)
        row.prop(s, "record_include_shake", text="Screen shake")
        row.prop(s, "record_end_frame")
        if s.record_mode == record.RecordMode.STEP and runtime.control_kind() == emucontrol.NONE:
            col.label(text="Frame by frame needs the DevServer or Dolphin hotkeys", icon="INFO")
        row = layout.row()
        row.scale_y = 1.4
        row.enabled = self.connected() or recording
        if recording:
            row.operator("ebc.record_stop", text="Stop", icon="SNAP_FACE")
        else:
            row.operator("ebc.record_start", text="Record", icon="REC")
        draw_record_stats(layout, recording)


def draw_record_stats(layout, recording: bool) -> None:
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
        layout.separator()
        row = layout.row(align=True)
        row.prop(s, "hand_back_to_game")
        row.operator("ebc.hand_back", text="", icon="KEYFRAME_HLT")


# -- Stages -----------------------------------------------------------------------------------------


class EBC_UL_stages(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        s = data
        current = s.stage_key and stage_presets.matching_preset(s, s.stage_key) == item
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        sub = row.row(align=True)
        sub.active = item.enabled
        sub.prop(item, "name", text="", emboss=False, icon="PLAY" if current else "WORLD")
        sub.prop(item, "camera", text="", icon="CAMERA_DATA")
        sub.prop(item, "collection", text="", icon="OUTLINER_COLLECTION")
        sub.prop(item, "match_variants", text="", icon="LINKED" if item.match_variants else "UNLINKED")


class EBC_PT_stages(_EBCPanel, Panel):
    bl_idname = "EBC_PT_stages"
    bl_label = "Stages"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        row = layout.row(align=True)
        row.label(text=f"Stage: {stage_presets.stage_label(s)}", icon="WORLD")
        sub = row.row(align=True)
        sub.enabled = self.connected()
        sub.operator("ebc.stage_identify", text="", icon="FILE_REFRESH")
        row = layout.row(align=True)
        row.prop(s, "stage_presets_enabled", text="Use presets")
        row.prop(s, "stage_collections_only", text="Only its collection")
        row = layout.row()
        row.template_list("EBC_UL_stages", "", s, "stage_presets", s, "stage_presets_index", rows=3)
        col = row.column(align=True)
        col.operator("ebc.stage_bind", text="", icon="ADD")
        col.operator("ebc.stage_add", text="", icon="VIEWZOOM")
        col.operator("ebc.stage_remove", text="", icon="REMOVE")
        if 0 <= s.stage_presets_index < len(s.stage_presets):
            draw_stage_row(layout, s.stage_presets[s.stage_presets_index])
        row = layout.row(align=True)
        row.operator("ebc.stage_bind", icon="PINNED")
        row.operator("ebc.stage_apply", text="Apply", icon="CHECKMARK")
        row.operator("ebc.stage_show_all", text="", icon="HIDE_OFF")


def draw_stage_row(layout, item) -> None:
    """The selected row's details: key, variants, and what it overrides on its stage (empty / 0 = the
    Camera panel's objects, the Timeline's Pause at frame)."""
    col = layout.column(align=True)
    col.prop(item, "key")
    row = col.row(align=True)
    row.prop(item, "match_variants")
    row.prop(item, "pause_frame", text="Pause at")
    for obj_prop, which, text in (("near_object", "NEAR", "Near"), ("far_object", "FAR", "Far")):
        row = col.row(align=True)
        row.prop(item, obj_prop, text=text, icon="OBJECT_DATA")
        if getattr(item, obj_prop) is None:
            op = row.operator("ebc.clip_helper", text="", icon="ADD")
            op.which = which
            op.stage_row = True


class EBC_PT_stage_load(_EBCPanel, Panel):
    bl_idname = "EBC_PT_stage_load"
    bl_label = "Load stage / stage views"
    bl_parent_id = "EBC_PT_stages"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = props.settings(context)
        row = layout.row(align=True)
        row.enabled = self.connected()
        row.label(text="Camera from stage view:")
        row.operator("ebc.stage_camera", text="Wide").view = "WIDE"
        row.operator("ebc.stage_camera", text="Fixed").view = "FIXED"
        col = layout.column(align=True)
        col.label(text="Reload the match on another stage (VS only, same fighters):")
        row = col.row(align=True)
        row.prop(s, "stage_to_load", text="")
        row.prop(s, "stage_variant", text="")
        row = col.row()
        row.scale_y = 1.3
        row.operator("ebc.stage_load", icon="FILE_REFRESH")
        why = stages.load_blocked()
        if why:
            col.label(text=why, icon="INFO")
        msg = stages.status_message()
        if msg:
            col.label(text=msg, icon="INFO")


classes = (
    EBC_PT_connection,
    EBC_PT_connection_options,
    EBC_PT_rig,
    EBC_PT_camera,
    EBC_PT_camera_advanced,
    EBC_PT_timeline,
    EBC_PT_emulator,
    EBC_PT_emulator_options,
    EBC_PT_green_screen,
    EBC_PT_fighter_tint,
    EBC_PT_display,
    EBC_PT_shadows,
    EBC_PT_audio,
    EBC_UL_cameras,
    EBC_PT_cameras,
    EBC_UL_stages,
    EBC_PT_stages,
    EBC_PT_stage_load,
    EBC_PT_record,
    EBC_PT_transitions,
)
