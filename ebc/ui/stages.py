"""Stages: per-stage presets applied on a new match, camera from the stage view, Reload on stage
(docs/USAGE.md "Stages"). The game side is ``core/stages.py``; the rows, the identity labels and the
enum items are in ``stage_presets``.

Presets are user data in the scene: one row per stage key (``01:BATTLEFIELD_L_BF_MELEE``) with an
optional camera and collection. When a stage is identified (a new match: the sync tick's poll, or
Identify), the matching row's camera becomes the scene camera (a clean cut, like a click on its dot
in the Cameras list) and, with "Show only the matching stage collection", its collection is shown
and the other rows' collections are hidden. Collections that are in no row are never touched.
A row may also set its own Near / Far objects and Pause at frame: the sync tick reads them on every
tick (``stage_presets.effective_overrides``), they are not applied here.
"""

import math
import time
import traceback
from dataclasses import dataclass
from enum import Enum

import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Operator
from mathutils import Matrix

from .. import runtime
from ..core import camera as cam_math
from ..core import game as g
from ..core import stages as st
from . import cameras, props, sync, util
from .stage_presets import (
    apply_collections,
    ensure_preset,
    matching_preset,
    parse_item,
    preset_label,
    profile_name,
    set_identity,
    stage_items,
)

RELOAD_WAIT_S = 8.0  # the intro lasts 3.5 s: a request made during it waits for the fight
RELOAD_LOAD_S = 10.0  # stage-reload.md 4.3: never seen over 1.3 s, error after 10 s, no retry
RELOAD_INTERVAL = 0.05


class Phase(Enum):
    WAIT = "wait"  # the request waits for the fight (stage intro)
    LOAD = "load"  # written: the game reloads the match


@dataclass
class Reload:
    """The reload under way (one at a time)."""

    slot: int
    button: int
    label: str
    phase: Phase
    deadline: float  # time.monotonic() at which the phase gives up
    matches: int | None = None  # Game.match_count when the request was written


_reload: Reload | None = None
_status_message: str | None = None


def status_message() -> str | None:
    return _status_message


# -- a stage identified ----------------------------------------------------------------------


def on_poll(s, scene, result) -> None:
    """After every poll: a stage identified (new match, Connect, reload) applies its preset."""
    if not result.stage_changed:
        return
    set_identity(s, result.stage)
    if result.stage is not None and s.stage_presets_enabled:
        apply_preset(s, scene, bpy.context.view_layer)


sync.on_stage_changed(on_poll)  # the sync tick calls it after its poll (sync never imports this module)


def activate_camera(s, scene, cam) -> None:
    """The camera EBC sends from now on, with a clean cut (it is added to the Cameras list)."""
    cameras.ensure_listed(s, cam)
    if scene.camera != cam:
        scene.camera = cam
    sync.request_cut()


def apply_preset(s, scene, view_layer) -> object:
    """The row of the current stage: its camera, its collection. Returns the row (or None)."""
    row = matching_preset(s, s.stage_key)
    if row is None:
        return None
    if row.camera is not None and row.camera.type == "CAMERA":
        activate_camera(s, scene, row.camera)
    if s.stage_collections_only:
        apply_collections(s, view_layer, s.stage_key)
    return row


# -- reload -----------------------------------------------------------------------------------


def load_blocked(game=None) -> str | None:
    """Why Load stage is greyed out, from what the sync / Identify last saw (no I/O), else None."""
    game = game or runtime.get_game()
    if not game.connected:
        return "Not connected"
    if _reload is not None:
        return "Reload under way"
    if game.stage is None:
        return "Not in a match (start the sync, or Identify the stage)"
    if game.match_vs is False:
        return "Not a VS match (Training, replay, other modes): not supported"
    return None


def _set_status(message: str) -> None:
    global _status_message
    _status_message = message


def _notify(level: str, message: str) -> None:
    _set_status(message)
    util.notify(level, message)
    util.tag_redraw()


def _finish(level: str, message: str) -> None:
    """The reload ends (done, cancelled or failed): the message in the panel and the status bar."""
    global _reload
    _reload = None
    _notify(level, message)


def reload_busy() -> bool:
    return _reload is not None


def start_reload(slot: int, button: int, label: str) -> tuple[str, str]:
    """Request a reload: (level, message). Runs at once during the fight, waits during the intro."""
    global _reload
    if _reload is not None:
        return "WARNING", "A reload is already under way"
    game = runtime.get_game()
    status, info = st.reload_check(game)
    if status == st.RELOAD_REFUSED:
        return "WARNING", f"Reload on stage refused: {info}"
    _reload = Reload(slot, button, label, Phase.WAIT, time.monotonic() + RELOAD_WAIT_S)
    if status == st.RELOAD_READY:
        if not _write(game, _reload, info):
            _reload = None
            return "WARNING", f"Reload on stage: write failed ({game.last_error})"
        msg = f"Reloading on {label}…"
    else:
        msg = f"Reload on {label}: {info}"
    _set_status(msg)
    if not bpy.app.timers.is_registered(_reload_timer):
        bpy.app.timers.register(_reload_timer, first_interval=RELOAD_INTERVAL)
    return "INFO", msg


def _write(game: g.Game, reload: Reload, ctx) -> bool:
    if not st.reload_write(game, ctx, reload.slot, reload.button):
        return False
    reload.phase = Phase.LOAD
    reload.deadline = time.monotonic() + RELOAD_LOAD_S
    reload.matches = game.match_count
    return True


def _reload_timer():
    try:
        return _reload_step()
    except Exception as exc:  # never raise from a timer  # noqa: BLE001
        traceback.print_exc()
        _finish("ERROR", f"Reload on stage failed: {exc}")
        return None


def _reload_step():
    global _reload
    game = runtime.get_game()
    reload = _reload
    if reload is None or not game.connected:
        _reload = None
        return None
    if reload.phase == Phase.WAIT:
        return _wait_step(game, reload)
    return _load_step(game, reload)


def _wait_step(game: g.Game, reload: Reload):
    """Waiting for the fight: write the request as soon as the game accepts it."""
    status, info = st.reload_check(game)
    if status == st.RELOAD_READY:
        if not _write(game, reload, info):
            _finish("ERROR", f"Reload on stage: write failed ({game.last_error})")
            return None
        _set_status(f"Reloading on {reload.label}…")
        return RELOAD_INTERVAL
    if status == st.RELOAD_REFUSED or time.monotonic() > reload.deadline:
        _finish("ERROR", f"Reload on {reload.label} cancelled: {info}")
        return None
    return RELOAD_INTERVAL


def _load_step(game: g.Game, reload: Reload):
    """Written: done once the new match runs and EBC's match detection saw it."""
    now = time.monotonic()
    # loading: the sync tick polls (new match -> settings and stage preset); without it, poll here
    if not sync.is_running():
        scene = bpy.context.scene
        result = game.poll()
        if scene is not None:
            on_poll(props.settings_of(scene), scene, result)
    # done once the new match runs AND EBC's match detection saw it (settings and preset applied)
    if game.match_count != reload.matches and st.reload_progress(game, reload.slot):
        stage = game.stage
        _finish("INFO", f"Reloaded on {stage.label if stage else reload.label}")
        return None
    if now > reload.deadline:
        _finish("ERROR", f"Reload on {reload.label}: the new match did not start within {RELOAD_LOAD_S:.0f} s")
        return None
    return RELOAD_INTERVAL


def cancel_reload() -> None:
    global _reload
    _reload = None
    if bpy.app.timers.is_registered(_reload_timer):
        bpy.app.timers.unregister(_reload_timer)


# -- operators ---------------------------------------------------------------------------------


class EBC_OT_stage_identify(Operator):
    """Read which stage is being played now (the sync does it on every new match)"""

    bl_idname = "ebc.stage_identify"
    bl_label = "Identify stage"

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected

    def execute(self, context):
        s = props.settings(context)
        stage = runtime.get_game().identify_stage()
        set_identity(s, stage)
        if stage is None:
            self.report({"WARNING"}, "No stage: not in a match (or still loading)")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Stage: {stage.label}")
        return {"FINISHED"}


class EBC_OT_stage_bind(Operator):
    """Add (or update) the row of the stage being played: the scene's active camera and the collection
    active in the Outliner (either may be empty)"""

    bl_idname = "ebc.stage_bind"
    bl_label = "Bind current stage"
    bl_options = {"REGISTER", "UNDO"}

    pause_here: BoolProperty(
        name="Pause at the current frame",
        description="Also set the row's Pause at frame to the game frame shown now (frames since GO)",
        default=False,
    )
    clip_objects: BoolProperty(
        name="Near / Far objects",
        description="Also give the row the Camera panel's Near / Far objects (they become this stage's own)",
        default=False,
    )

    def execute(self, context):
        s = props.settings(context)
        game = runtime.get_game()
        stage = game.stage
        if stage is None and game.connected:
            stage = game.identify_stage()
            set_identity(s, stage)
        key = stage.key if stage is not None else s.stage_key
        if not key:
            self.report({"WARNING"}, "No stage identified: connect and load a match first (or use Add stage)")
            return {"CANCELLED"}
        row = ensure_preset(s, key, preset_label(stage, key))
        cam = context.scene.camera
        if cam is not None and cam.type == "CAMERA":
            row.camera = cam
        layer = context.view_layer.active_layer_collection
        coll = layer.collection if layer is not None else None
        if coll is not None and coll != context.scene.collection:
            row.collection = coll
        if self.pause_here and s.frame_number > 0:
            row.pause_frame = s.frame_number
        if self.clip_objects:
            row.near_object, row.far_object = s.near_object, s.far_object
        parts = (row.camera.name if row.camera else "", row.collection.name if row.collection else "")
        if row.pause_frame:
            parts += (f"pause at {row.pause_frame}",)
        bound = ", ".join(x for x in parts if x)
        self.report({"INFO"}, f"{row.name} ({key}): {bound or 'empty row'}")
        return {"FINISHED"}


class EBC_OT_stage_add(Operator):
    """Add a row for a stage picked in the list of the game version (to prepare it offline)"""

    bl_idname = "ebc.stage_add"
    bl_label = "Add stage"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "stage"

    stage: EnumProperty(name="Stage", items=stage_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        s = props.settings(context)
        slot, button = parse_item(self.stage)
        variant = next((v for v in st.reload_choices(profile_name()) if v.slot == slot and v.button == button), None)
        if variant is None:
            return {"CANCELLED"}
        ensure_preset(s, st.identity_key(slot, variant.key_stem), variant.name)
        return {"FINISHED"}


class EBC_OT_stage_remove(Operator):
    """Remove the selected row (its camera and collection are kept)"""

    bl_idname = "ebc.stage_remove"
    bl_label = "Remove stage row"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        s = props.settings(context)
        return 0 <= s.stage_presets_index < len(s.stage_presets)

    def execute(self, context):
        s = props.settings(context)
        util.remove_active(s, "stage_presets", "stage_presets_index")
        return {"FINISHED"}


class EBC_OT_stage_apply(Operator):
    """Apply the row of the stage being played now (camera, collection)"""

    bl_idname = "ebc.stage_apply"
    bl_label = "Apply stage preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = props.settings(context)
        row = apply_preset(s, context.scene, context.view_layer)
        if row is None:
            self.report({"WARNING"}, f"No row for {s.stage_key or 'the current stage'}")
            return {"CANCELLED"}
        return {"FINISHED"}


class EBC_OT_stage_show_all(Operator):
    """Show every collection of the Stages list again (viewport and render)"""

    bl_idname = "ebc.stage_show_all"
    bl_label = "Show all stage collections"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        apply_collections(props.settings(context), context.view_layer, None)
        return {"FINISHED"}


VIEWS = [
    ("WIDE", "Wide view", "The stage's whole camera range, framed by the game's own match-camera rules"),
    ("FIXED", "Fixed camera", "The stage's fixed camera (the one the 'fixed camera' stage option uses)"),
]


class EBC_OT_stage_camera(Operator):
    """Create a Blender camera at a view of the stage being played (a starting point, whatever the
    fighters): the wide view or the stage's fixed camera"""

    bl_idname = "ebc.stage_camera"
    bl_label = "Camera from stage view"
    bl_options = {"REGISTER", "UNDO"}

    view: EnumProperty(name="View", items=VIEWS, default="WIDE")

    @classmethod
    def description(cls, context, properties):
        desc = {ident: desc for ident, _name, desc in VIEWS}.get(properties.view)
        return f"New camera: {desc}" if desc else cls.__doc__

    @classmethod
    def poll(cls, context):
        return runtime.get_game().connected

    def execute(self, context):
        s = props.settings(context)
        game = runtime.get_game()
        data = st.read_stage_camera(game)
        if data is None:
            self.report({"WARNING"}, "No stage camera data: load a match first")
            return {"CANCELLED"}
        eye, target, fov = st.wide_view(data) if self.view == "WIDE" else st.fixed_view(data)
        stage = game.stage or game.identify_stage()
        tag = stage.stem.title().replace("_", "") if stage else "Stage"
        name = f"EBC_{tag}_{'Wide' if self.view == 'WIDE' else 'Fixed'}"
        cam = cameras.new_camera(context, name, None)
        cam.matrix_world = Matrix(cam_math.blender_matrix_from_game(eye, target, 0.0))
        cam.data.lens = cameras.lens_for(cam, context.scene, fov)
        cameras.ensure_listed(s, cam)
        if stage is not None:
            set_identity(s, stage)
            row = matching_preset(s, stage.key)
            if row is not None and row.camera is None:
                row.camera = cam
        eye_text = tuple(round(v, 2) for v in eye)
        self.report({"INFO"}, f"{cam.name}: eye {eye_text} (Brawl), FOV {math.degrees(fov):.1f}°")
        return {"FINISHED"}


class EBC_OT_stage_load(Operator):
    """Reload the match on the Stage to load (and its variant), same fighters, costumes and rules, by
    memory writes only. A new match: damage, stocks and timer start again. VS matches only; during the
    stage intro it waits for the fight"""

    bl_idname = "ebc.stage_load"
    bl_label = "Load stage"

    @classmethod
    def poll(cls, context):
        why = load_blocked()
        if why is not None:
            cls.poll_message_set(why)
        return why is None

    def execute(self, context):
        s = props.settings(context)
        slot = int(s.stage_to_load, 16)
        button = int(s.stage_variant, 16) if s.stage_variant else 0
        variant = next((v for v in st.table_for(profile_name()).get(slot, ("", ()))[1] if v.button == button), None)
        label = variant.name if variant else f"slot {slot:02X}"
        level, msg = start_reload(slot, button, label)
        self.report({level}, msg)
        return {"FINISHED"} if level == "INFO" else {"CANCELLED"}


classes = (
    EBC_OT_stage_identify,
    EBC_OT_stage_bind,
    EBC_OT_stage_add,
    EBC_OT_stage_remove,
    EBC_OT_stage_apply,
    EBC_OT_stage_show_all,
    EBC_OT_stage_camera,
    EBC_OT_stage_load,
)
