"""Scene settings (``Scene.ebc``).

Properties are only ever read as attributes (``self.prop``): since Blender 5.0
registered properties are no longer reachable through ``self["prop"]``.

Every ``update=`` callback goes through ``_safe``: it calls the core, which is
a no-op while disconnected, and never lets an exception escape.
"""

import functools
import traceback
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, PropertyGroup

from .. import runtime
from ..core import display as disp
from ..core import dolphin_config, dsu
from ..core import fighters as ft
from ..core import freeze as fz
from ..core.addresses import PROFILES
from . import cameras, record, stage_presets

SLOTS = (0, 1, 2, 3)
# The four per-slot properties of EBCSettings (Blender needs one property each; .blend files use these names).
PLAYER_PROPS = ("player_1", "player_2", "player_3", "player_4")
TINT_PROPS = ("tint_p1", "tint_p2", "tint_p3", "tint_p4")


# Exceptions swallowed by update callbacks, kept for the smoke test.
callback_errors: list[str] = []


def _safe(fn):
    @functools.wraps(fn)
    def update(self, context):
        try:
            fn(self, context)
        except Exception as exc:  # never break Blender's UI from an update callback  # noqa: BLE001
            traceback.print_exc()
            callback_errors.append(f"{fn.__name__}: {exc!r}")

    return update


def _is_camera(_self, obj) -> bool:
    return obj is not None and obj.type == "CAMERA"


def settings_of(scene) -> "EBCSettings":
    return scene.ebc  # type: ignore[attr-defined]


def settings(context=None) -> "EBCSettings":
    return settings_of((context or bpy.context).scene)


# -- update callbacks ------------------------------------------------------------


def _upd_direction(self, _context):
    # Blender -> Brawl freezes the game camera (Game.freeze_camera, native or Code Menu); Brawl -> Blender
    # gives it back.
    want_lock = self.camera_direction == "BLENDER_TO_GAME"
    if self.lock_game_camera != want_lock:
        self.lock_game_camera = want_lock  # its own update writes the flag
    else:
        runtime.get_game().freeze_camera(want_lock)


def _upd_near(self, _context):
    runtime.get_game().set_front_depth(self.near_clip)


def _upd_far(self, _context):
    runtime.get_game().set_back_depth(self.far_clip)


def _upd_hud(self, _context):
    runtime.get_game().set_hud(self.hud)


def _upd_debug_menu(self, _context):
    runtime.get_game().set_debug_menu(self.debug_menu)  # off also resumes a game pause
    if not self.debug_menu and self.game_pause:
        self.game_pause = False  # the buttons drive Dolphin again


def _upd_game_pause(self, _context):
    if self.game_pause:
        if not self.debug_menu:
            self.debug_menu = True  # its own update turns the Code Menu Debug Mode on
    else:
        runtime.get_game().debug.resume()  # Play / Pause drives Dolphin again: leave no game stopped


def _upd_draw_di(self, _context):
    runtime.get_game().set_draw_di(self.draw_di)


def _upd_lock_game_camera(self, _context):
    runtime.get_game().freeze_camera(self.lock_game_camera)


def _upd_freeze_method(self, _context):
    runtime.get_game().set_freeze_method(self.freeze_method)


def _upd_characters(self, _context):
    runtime.get_game().set_characters_display(self.characters_display)


def _upd_stage(self, _context):
    runtime.get_game().set_stage_display(self.stage_display)


def _upd_music(self, _context):
    runtime.get_game().set_music_volume(self.music_volume)


def _upd_sfx(self, _context):
    runtime.get_game().set_sfx_volume(self.sfx_volume)


def _upd_light_x(self, _context):
    runtime.get_game().set_shadow_direction(x=self.light_angle_x)


def _upd_light_y(self, _context):
    runtime.get_game().set_shadow_direction(y=self.light_angle_y)


def green_screen_rgba(s) -> tuple[float, float, float, float]:
    """The Full-screen background colour sent to the game: opaque while a fighter is isolated."""
    r, gr, b, a = tuple(s.green_screen_color)
    return (r, gr, b, 1.0 if s.isolate_saved else a)


def _upd_green_screen(self, _context):
    runtime.get_game().set_green_screen(self.green_screen, green_screen_rgba(self))


def update_green_screen(s) -> None:
    """Send the Full-screen background again (Isolate changes how its alpha is sent)."""
    _safe(_upd_green_screen)(s, None)


def _upd_stage_void(self, _context):
    runtime.get_game().set_stage_void_color(self.stage_void, tuple(self.stage_void_color))


def _upd_sync_fov(self, _context):
    # Turning Sync FOV off gives the FOV back to the stage (its measured default).
    if not self.sync_fov:
        runtime.get_game().clear_fov()


def _upd_copy_roll(self, _context):
    if not self.copy_camera_roll:
        runtime.get_game().clear_camera_roll()


# -- Projection: a view of the synced camera's own type (no value stored here) ----------------

PROJECTIONS = ("PERSP", "ORTHO")


def _get_projection(self) -> int:
    try:
        cam = cameras.synced_camera(self)
        return 1 if cam is not None and cam.data.type == "ORTHO" else 0
    except Exception as exc:  # a getter runs in draw(): never break the panel, but keep the trace  # noqa: BLE001
        traceback.print_exc()
        callback_errors.append(f"_get_projection: {exc!r}")
        return 0


def _set_projection(self, value: int) -> None:
    try:
        cam = cameras.synced_camera(self)
        if cam is None:
            return
        want = PROJECTIONS[value]
        if cam.data.type == want:
            return
        if want == "ORTHO" and cam.data.type == "PERSP":
            cameras.keep_framing_ortho(cam, self.id_data)
        cam.data.type = want
    except Exception as exc:  # never break Blender's UI from a property setter  # noqa: BLE001
        traceback.print_exc()
        callback_errors.append(f"_set_projection: {exc!r}")


def _upd_devserver_port(self, _context):
    runtime.devserver.set_port(self.devserver_port)


def _upd_skip_intro(self, _context):
    game = runtime.get_game()
    if self.skip_stage_intro and game.connected:
        runtime.devserver.set_port(self.devserver_port)
        runtime.devserver.status()  # the route: code patch when the DevServer answers, else data
    game.set_intro_skip(self.skip_stage_intro)


def _stage_slot_items(self, context):
    return stage_presets.slot_items(self, context)


def _stage_variant_items(self, context):
    return stage_presets.variant_items(self.stage_to_load)


def _upd_stage_collections(self, _context):
    stage_presets.apply_collections(
        self, bpy.context.view_layer, self.stage_key if self.stage_collections_only else None
    )


def tint_rgba(tint: "EBCTint") -> tuple[float, float, float, float]:
    r, gg, b = tuple(tint.color)
    return (r, gg, b, tint.alpha / 255.0)


def _upd_tint(self, _context):
    # ``self`` is the EBCTint; its path ("ebc.tint_p2") tells which slot it is. (No stored slot: an
    # update may not write a property, and .blend files saved before would not have it.)
    slot = TINT_PROPS.index(self.path_from_id().rpartition(".")[2])
    runtime.get_game().set_fighter_mode(slot, self.mode, tint_rgba(self))


# -- property groups --------------------------------------------------------------


class EBCTint(PropertyGroup):
    mode: EnumProperty(
        name="Mode",
        description="How this character is drawn (experimental)",
        items=[
            (ft.FIGHTER_NORMAL, "Normal", "Leave the character as the game draws it"),
            (ft.FIGHTER_COLOUR, "Colour", "Tint the character with the colour and alpha"),
            (ft.FIGHTER_HIDDEN, "Hidden", "Do not draw the character (its effects stay visible)"),
        ],
        default=ft.FIGHTER_NORMAL,
        update=_safe(_upd_tint),
    )
    color: FloatVectorProperty(
        name="Colour",
        subtype="COLOR_GAMMA",
        size=3,
        min=0.0,
        max=1.0,
        default=(0.19, 1.0, 0.38),
        update=_safe(_upd_tint),
    )
    alpha: IntProperty(
        name="Alpha",
        description="Tint strength (0 = invisible, 255 = flat colour)",
        min=0,
        max=255,
        default=192,
        update=_safe(_upd_tint),
    )


class EBCCameraItem(PropertyGroup):
    camera: PointerProperty(name="Camera", type=bpy.types.Object, poll=_is_camera)


class EBCStagePreset(PropertyGroup):
    """One row of the Stages list: what to do when this stage is loaded. ``name`` is the label."""

    key: StringProperty(
        name="Stage key",
        description="Slot and .pac of the stage (as shown in the Connection panel), e.g. 01:BATTLEFIELD",
    )
    camera: PointerProperty(
        name="Camera",
        description="Camera activated when this stage is loaded (a clean cut); empty = keep the current one",
        type=bpy.types.Object,
        poll=_is_camera,
    )
    collection: PointerProperty(
        name="Collection",
        description="Collection shown for this stage (the other rows' collections are hidden)",
        type=bpy.types.Collection,
    )
    match_variants: BoolProperty(
        name="Match variants",
        description="Also use this row for the other variants of the slot (L, R, Z, alternates) without a row",
        default=False,
    )
    enabled: BoolProperty(name="Enabled", description="Use this row", default=True)
    near_object: PointerProperty(
        name="Near object",
        description="Near object used on this stage instead of the Camera panel's one; empty = the Camera panel's",
        type=bpy.types.Object,
    )
    far_object: PointerProperty(
        name="Far object",
        description="Far object used on this stage instead of the Camera panel's one; empty = the Camera panel's",
        type=bpy.types.Object,
    )
    pause_frame: IntProperty(
        name="Pause at frame",
        description=(
            "Pause the emulation once when the game frame (frames since GO) reaches this value on this stage, "
            "while syncing. 0 = the Timeline panel's Pause at frame"
        ),
        default=0,
        min=0,
    )


class EBCSettings(PropertyGroup):
    # -- Rig ---------------------------------------------------------------------
    camera: PointerProperty(
        name="Camera", description="Blender camera driving the game camera", type=bpy.types.Object, poll=_is_camera
    )
    origin: PointerProperty(
        name="Origin",
        description=(
            "Aim point shown in Blender (a Track To target, for instance). Not needed to aim: the game camera "
            "looks along the Blender camera's view axis. Brawl → Blender moves it onto the game's target"
        ),
        type=bpy.types.Object,
    )
    player_1: PointerProperty(name="Player 1", type=bpy.types.Object)
    player_2: PointerProperty(name="Player 2", type=bpy.types.Object)
    player_3: PointerProperty(name="Player 3", type=bpy.types.Object)
    player_4: PointerProperty(name="Player 4", type=bpy.types.Object)

    # -- Camera ------------------------------------------------------------------
    camera_direction: EnumProperty(
        name="Direction",
        items=[
            ("BLENDER_TO_GAME", "Blender → Brawl", "The Blender camera drives the game camera"),
            ("GAME_TO_BLENDER", "Brawl → Blender", "The game camera is copied to the Blender camera"),
        ],
        default="BLENDER_TO_GAME",
        update=_safe(_upd_direction),
    )
    sync_fov: BoolProperty(
        name="Sync FOV",
        description=(
            "Send the camera's vertical field of view to the game every tick (also to the stage FOV, "
            "so the game does not ease it back). Off gives the stage its own FOV back"
        ),
        default=False,
        update=_safe(_upd_sync_fov),
    )
    near_clip: IntProperty(
        name="Near clip",
        description="Nothing closer than this is drawn",
        default=1,
        min=1,
        max=1000,
        update=_safe(_upd_near),
    )
    far_clip: IntProperty(
        name="Far clip",
        description="Nothing farther than this is drawn (maximum = infinite)",
        default=5000,
        min=1,
        max=5000,
        update=_safe(_upd_far),
    )
    near_object: PointerProperty(
        name="Near object",
        description=(
            "Near clip = this object's distance in front of the synced camera, along its view axis, every tick "
            "(overrides the slider). + creates one parented to the camera: near clip = minus its local Z"
        ),
        type=bpy.types.Object,
    )
    far_object: PointerProperty(
        name="Far object",
        description=(
            "Far clip = this object's distance in front of the synced camera, along its view axis, every tick "
            "(overrides the slider; beyond the maximum = infinite). + creates one parented to the camera: "
            "far clip = minus its local Z"
        ),
        type=bpy.types.Object,
    )
    keep_origin_on_plane: BoolProperty(
        name="Keep origin on the stage plane",
        description=(
            "Code Menu (legacy) freeze only: pin the origin to the stage plane (Blender Y = 0), where the game "
            "keeps the aim, and centre the camera view on it"
        ),
        default=False,
    )
    copy_camera_roll: BoolProperty(
        name="Copy camera roll",
        description="Send the camera roll (read from its world orientation, Track To included) to the game",
        default=False,
        update=_safe(_upd_copy_roll),
    )
    projection: EnumProperty(
        name="Projection",
        description="Projection of the synced camera (its own type): the game follows it",
        items=[
            ("PERSP", "Perspective", "Perspective camera (the game's default)", 0),
            ("ORTHO", "Orthographic", "Orthographic camera: the game draws in orthographic too", 1),
        ],
        get=_get_projection,
        set=_set_projection,
    )
    axonometric_azimuth: EnumProperty(
        name="Yaw",
        description="Direction the Isometric / Dimetric views look from (Blender rotation Z)",
        items=[
            ("45", "45°", "From behind the stage, its left side (as the game shows it)"),
            ("135", "135°", "From the front, the stage's left side (as the game shows it)"),
            ("225", "225°", "From the front, the stage's right side (as the game shows it)"),
            ("315", "315°", "From behind the stage, its right side (as the game shows it)"),
        ],
        default="135",
    )
    disable_screen_shake: BoolProperty(
        name="Disable screen shake",
        description="Cancel the game's screen shake (hits, explosions) on every tick while syncing",
        default=False,
    )
    show_player_positions: BoolProperty(
        name="Show player positions",
        description="Move the player objects to the fighters' positions, hide empty slots",
        default=False,
    )

    # -- Timeline ----------------------------------------------------------------
    timeline_mode: EnumProperty(
        name="Mode",
        items=[
            ("FOLLOW", "Follow game frame", "The Blender timeline follows the game frame counter"),
            ("START_AT", "Start at frame", "Start playing the Blender animation when the game reaches a frame"),
            ("OFF", "Off", "Do not touch the Blender timeline"),
        ],
        default="FOLLOW",
    )
    start_frame: IntProperty(
        name="Start frame", description="Game frame on which the Blender animation starts", default=1, min=0
    )
    pause_frame: IntProperty(
        name="Pause at frame",
        description=(
            "Pause the emulation once when the game frame (frames since GO) reaches this value, while syncing "
            "(again after a new match, or a savestate from before it). 0 = off. A Stages row can set its own. "
            "Game pause (debug mode) or DevServer: on that frame; Dolphin hotkeys: may stop 1-2 frames later"
        ),
        default=0,
        min=0,
    )
    game_pause: BoolProperty(
        name="Game pause (debug mode)",
        description=(
            "Pause / Resume and Next frame stop and step the GAME through P+'s debug pause (turns the Code "
            "Menu Debug Mode on): Dolphin keeps running, so the camera sync still shows in the picture, and "
            "Next frame is exactly one game frame. Off: they drive Dolphin's emulator pause"
        ),
        default=False,
        update=_safe(_upd_game_pause),
    )
    state_slot: EnumProperty(
        name="State slot",
        items=[("1", "1", "State slot 1"), ("2", "2", "State slot 2"), ("3", "3", "State slot 3")],
        default="1",
    )
    game_version: EnumProperty(
        name="Game version",
        description="Project+ build whose memory addresses EBC uses. Applied on the next Connect",
        items=[("auto", "Auto-detect", "Recognise the build from its Gecko codesets on Connect")]
        + [(name, prof.label, f"Always use the {prof.label} addresses") for name, prof in PROFILES.items()],
        default="auto",
    )
    devserver_port: IntProperty(
        name="DevServer port",
        description="TCP port of the Dolphin DevServer on 127.0.0.1",
        default=27100,
        min=1,
        max=65535,
        update=_safe(_upd_devserver_port),
    )
    screenshot_folder: StringProperty(
        name="Screenshot folder",
        description=(
            "Where Screenshot puts the captures, renamed <stage>_<game frame>_<date-time>.png. Empty = "
            "Dolphin's own screenshot folder (<Dolphin user folder>/ScreenShots/<game ID>). // = next to "
            "the .blend (an unsaved file uses the temporary folder)"
        ),
        default="",
        subtype="DIR_PATH",
    )

    # -- Green screen --------------------------------------------------------------
    green_screen: BoolProperty(
        name="Full-screen background",
        description=(
            "Lay a flat colour over the stage, under the fighters, items, effects and HUD (the game's own "
            "full-screen fill). The colour's alpha is effective: 0 = invisible, 1 = a flat green screen"
        ),
        default=False,
        update=_safe(_upd_green_screen),
    )
    green_screen_color: FloatVectorProperty(
        name="Full-screen background colour",
        description="Colour and opacity (alpha) of the full-screen background",
        subtype="COLOR_GAMMA",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.0, 1.0, 0.0, 1.0),
        update=_safe(_upd_green_screen),
    )
    stage_void: BoolProperty(
        name="Void colour",
        description=(
            "Colour of the empty space where nothing is drawn: around the stage, and everywhere behind "
            "the fighters once the stage is hidden (the game's clear colour, black by default; no alpha)"
        ),
        default=False,
        update=_safe(_upd_stage_void),
    )
    stage_void_color: FloatVectorProperty(
        name="Void colour value",
        subtype="COLOR_GAMMA",
        size=3,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0),
        update=_safe(_upd_stage_void),
    )
    stage_display: EnumProperty(
        name="Stage",
        items=[
            (disp.STAGE_HIDDEN, "Hidden", "Hide the stage: the void colour shows behind the fighters"),
            (disp.STAGE_VISIBLE, "Visible", "Default stage"),
            (disp.STAGE_COLLISION_ONLY, "Collision only", "Only the collision geometry"),
            (disp.STAGE_VISIBLE_COLLISION, "Visible + collision", "Stage and collision geometry"),
        ],
        default=disp.STAGE_VISIBLE,
        update=_safe(_upd_stage),
    )
    tint_p1: PointerProperty(type=EBCTint)
    tint_p2: PointerProperty(type=EBCTint)
    tint_p3: PointerProperty(type=EBCTint)
    tint_p4: PointerProperty(type=EBCTint)
    # HUD, Full-screen background and Stage as they were before Isolate, put back by the second
    # press. While isolate_saved, the full-screen background is sent opaque (green_screen_rgba).
    isolate_saved: BoolProperty(default=False, options={"HIDDEN"})
    isolate_hud: BoolProperty(default=True, options={"HIDDEN"})
    isolate_green_screen: BoolProperty(default=False, options={"HIDDEN"})
    isolate_stage: StringProperty(default=disp.STAGE_VISIBLE, options={"HIDDEN"})

    # -- Display -------------------------------------------------------------------
    hud: BoolProperty(name="HUD", description="Stocks, damage, timer", default=True, update=_safe(_upd_hud))
    debug_menu: BoolProperty(
        name="Debug mode",
        description=(
            "Code Menu Debug Mode: P+'s debug controls in game (Start = game pause, Z = next frame, "
            "L + D-pad = hitboxes...). Turning it off resumes a game pause"
        ),
        default=False,
        update=_safe(_upd_debug_menu),
    )
    draw_di: BoolProperty(name="Draw DI", default=False, update=_safe(_upd_draw_di))
    lock_game_camera: BoolProperty(
        name="Lock game camera",
        description="Freeze the game camera so it shows what Blender sends (needed for Blender → Brawl)",
        default=False,
        update=_safe(_upd_lock_game_camera),
    )
    freeze_method: EnumProperty(
        name="Freeze method",
        description="How the game camera is frozen",
        items=[
            (
                fz.FREEZE_NATIVE,
                "Native",
                "Stop the game's match camera and write the whole camera: exact from the next frame, "
                "no shake, any aim, no Code Menu",
            ),
            (
                fz.FREEZE_CODE_MENU,
                "Code Menu (legacy)",
                "Project+ Code Menu camera lock: target forced onto the stage plane, shakes on hits",
            ),
        ],
        default=fz.FREEZE_NATIVE,
        update=_safe(_upd_freeze_method),
    )
    characters_display: EnumProperty(
        name="Characters",
        items=[
            (disp.CHARACTERS_MODEL, "Model", "Default"),
            (disp.CHARACTERS_MODEL_HURTBOXES, "Model + hurtboxes", "Model with hurtboxes"),
            (disp.CHARACTERS_HURTBOXES, "Hurtboxes only", "Only the hurtboxes"),
            (disp.CHARACTERS_HIDDEN, "Hidden", "Hide every character model"),
        ],
        default=disp.CHARACTERS_MODEL,
        update=_safe(_upd_characters),
    )

    # -- Shadows ---------------------------------------------------------------------
    light_angle_x: IntProperty(
        name="Light pitch",
        description="Shadow light pitch in degrees (stage default: 20 on Pokemon Stadium 2, 30 on Final Destination)",
        default=90,
        min=0,
        max=180,
        update=_safe(_upd_light_x),
    )
    light_angle_y: IntProperty(
        name="Light yaw",
        description="Shadow light yaw in degrees (stage default: 185 on Pokemon Stadium 2, 45 on Final Destination)",
        default=0,
        min=-180,
        max=360,
        update=_safe(_upd_light_y),
    )

    # -- Audio -----------------------------------------------------------------------
    music_volume: IntProperty(name="Music", default=100, min=0, max=100, subtype="PERCENTAGE", update=_safe(_upd_music))
    sfx_volume: IntProperty(
        name="Sound effects", default=100, min=0, max=100, subtype="PERCENTAGE", update=_safe(_upd_sfx)
    )

    # -- Cameras ---------------------------------------------------------------------
    cameras: CollectionProperty(type=EBCCameraItem)
    cameras_index: IntProperty(default=0)

    # -- Stages --------------------------------------------------------------------------
    skip_stage_intro: BoolProperty(
        name="Skip stage intro camera",
        description=(
            "No stage intro camera (the close-ups on each fighter before GO): the match camera, or the "
            "Blender camera while syncing, from the first frame. DevServer: a 3-word code patch, exact "
            "(removed before savestates and on disconnect). Stock Dolphin: data only, while EBC drives the "
            "camera; the roll stays 0 during the intro"
        ),
        default=False,
        update=_safe(_upd_skip_intro),
    )
    stage_presets: CollectionProperty(type=EBCStagePreset)
    stage_presets_index: IntProperty(default=0)
    stage_presets_enabled: BoolProperty(
        name="Use stage presets",
        description="When a stage is loaded, apply its row of the Stages list (camera, collection)",
        default=True,
    )
    stage_collections_only: BoolProperty(
        name="Show only the matching stage collection",
        description=(
            "Show the loaded stage's collection and hide the collections of the OTHER rows (viewport and "
            "render). Collections not in the list are never touched. Off shows them all again"
        ),
        default=False,
        update=_safe(_upd_stage_collections),
    )
    stage_to_load: EnumProperty(
        name="Stage to load",
        description="Project+ stage slot (ID) that Load stage reloads the match on",
        items=_stage_slot_items,
    )
    stage_variant: EnumProperty(
        name="Variant",
        description="ASL variant of the stage (the button held on the stage select screen); Base by default",
        items=_stage_variant_items,
    )
    # The stage being played (written by the sync / Identify; readable by drivers)
    stage_name: StringProperty(name="Stage name", description="Name of the stage being played (read only)")
    stage_slot: IntProperty(
        name="Stage slot", description="Project+ stage slot being played, -1 = none (read only)", default=-1
    )
    stage_key: StringProperty(
        name="Stage key", description="Slot and .pac of the stage being played, e.g. 01:BATTLEFIELD (read only)"
    )

    # -- Record ------------------------------------------------------------------------
    record_camera: PointerProperty(
        name="Into",
        description="Camera that receives the recorded keyframes (empty: create EBC_GameCamera)",
        type=bpy.types.Object,
        poll=_is_camera,
    )
    record_mode: EnumProperty(
        name="Mode",
        items=[
            (record.RecordMode.LIVE, "Live", "Record while the game runs at full speed (a missed frame is reported)"),
            (
                record.RecordMode.STEP,
                "Frame by frame",
                "Pause, read, advance one frame, repeat (DevServer; exact on a replay)",
            ),
        ],
        default=record.RecordMode.LIVE,
    )
    record_include_shake: BoolProperty(
        name="Include screen shake",
        description="Record the exact rendered orientation (screen-shake angles included) instead of eye/target/roll",
        default=False,
    )
    record_end_frame: IntProperty(
        name="End frame",
        description="Stop by itself once the game reaches this frame (0 = until Stop)",
        default=0,
        min=0,
    )

    # -- Transitions ---------------------------------------------------------------------
    transition_frames: IntProperty(
        name="Frames", description="Length of the transition, from the current frame", default=30, min=1, max=100000
    )
    transition_easing: EnumProperty(
        name="Easing",
        items=[
            ("BEZIER", "Ease in/out", "Smooth start and end (Bezier)"),
            ("SINE", "Sine", "Sinusoidal ease in/out"),
            ("CUBIC", "Cubic", "Cubic ease in/out"),
            ("LINEAR", "Linear", "Constant speed"),
        ],
        default="BEZIER",
    )
    transition_reverse: BoolProperty(
        name="Back to the game camera",
        description="From the selected camera to the recorded game camera, instead of the other way round",
        default=False,
    )
    hand_back_to_game: BoolProperty(
        name="Hand back to the game",
        description=(
            "While on, the sync releases the camera lock and sends nothing: the game eases from the last "
            "camera sent back to its own camera. Keyframe it to hand back at a given frame"
        ),
        default=False,
    )

    # -- Read-only display (written by the sync loop) ---------------------------------
    frame_number: IntProperty(name="Frame", default=0)
    stage_id: IntProperty(name="Stage", default=0)

    def tint(self, slot: int) -> "EBCTint":
        return getattr(self, TINT_PROPS[slot])

    def fighter_modes(self) -> list[str]:
        return [self.tint(slot).mode for slot in SLOTS]

    def player(self, slot: int):
        return getattr(self, PLAYER_PROPS[slot])


# -- pushing the panel state to the game -------------------------------------------

_TOGGLES = (
    ("hud", _upd_hud),
    ("debug_menu", _upd_debug_menu),
    ("draw_di", _upd_draw_di),
    ("freeze_method", _upd_freeze_method),  # before the lock: it picks the mechanism
    ("lock_game_camera", _upd_lock_game_camera),
    ("characters_display", _upd_characters),
    ("stage_display", _upd_stage),
    ("near_clip", _upd_near),
    ("far_clip", _upd_far),
    ("music_volume", _upd_music),
    ("sfx_volume", _upd_sfx),
    ("light_angle_x", _upd_light_x),
    ("light_angle_y", _upd_light_y),
    ("skip_stage_intro", _upd_skip_intro),
)


def _is_default(s: EBCSettings, name: str) -> bool:
    prop = s.bl_rna.properties[name]
    return getattr(s, name) == prop.default  # type: ignore[attr-defined]


def push_settings(s: EBCSettings, force: bool = False) -> None:
    """Send the panel state to the game (after connecting).

    Without ``force`` only values changed from their default are sent, so
    connecting does not, for example, close a debug menu opened in game.
    """
    for name, fn in _TOGGLES:
        if force or not _is_default(s, name):
            _safe(fn)(s, None)
    if s.green_screen:
        _safe(_upd_green_screen)(s, None)
    if s.stage_void:
        _safe(_upd_stage_void)(s, None)
    for slot in SLOTS:
        tint = s.tint(slot)
        if tint.mode != ft.FIGHTER_NORMAL:
            _safe(_upd_tint)(tint, None)
    remember_sent(s)


# Values last sent for each scene, to catch changes made without the panel. Blender
# does not call ``update=`` callbacks when a driver or an F-curve changes a property,
# so the sync tick compares and pushes what changed (no memory access otherwise).
_last_sent: dict[int, dict[str, object]] = {}


def _value(v):
    return tuple(v) if hasattr(v, "__len__") and not isinstance(v, str) else v


# Every panel setting whose update= callback talks to the game, grouped by callback.
# Settings read on every tick anyway (camera, lens, objects, Keep origin, screen shake,
# projection, timeline, Hand back) are not listed.
_WATCHED: tuple[tuple[tuple[str, ...], object], ...] = (
    *(((name,), fn) for name, fn in _TOGGLES),
    (("camera_direction",), _upd_direction),
    (("sync_fov",), _upd_sync_fov),
    (("copy_camera_roll",), _upd_copy_roll),
    (("green_screen", "green_screen_color"), _upd_green_screen),
    (("stage_void", "stage_void_color"), _upd_stage_void),
    (("devserver_port",), _upd_devserver_port),
)


def _snapshot(s: EBCSettings) -> dict[str, object]:
    snap: dict[str, object] = {}
    for names, _fn in _WATCHED:
        for name in names:
            snap[name] = _value(getattr(s, name))
    for slot in SLOTS:
        tint = s.tint(slot)
        snap[f"tint{slot}"] = (tint.mode, _value(tint.color), tint.alpha)
    return snap


def remember_sent(s: EBCSettings) -> None:
    """The panel state was just pushed in full (Connect, Re-apply)."""
    _last_sent[s.as_pointer()] = _snapshot(s)


def push_changed(s: EBCSettings) -> int:
    """Push the settings changed since the last tick (drivers, keyframes, scripts).

    Returns how many were pushed. The first call for a scene only records.
    """
    key = s.as_pointer()
    snap = _snapshot(s)
    last = _last_sent.get(key)
    _last_sent[key] = snap
    if last is None:
        return 0
    pushed = 0
    for names, fn in _WATCHED:
        if any(snap[n] != last.get(n) for n in names):
            _safe(fn)(s, None)
            pushed += 1
    for slot in SLOTS:
        if snap[f"tint{slot}"] != last.get(f"tint{slot}"):
            _safe(_upd_tint)(s.tint(slot), None)
            pushed += 1
    return pushed


# -- extension preferences ---------------------------------------------------------------

ADDON_ID = (__package__ or "ebc").rpartition(".")[0] or "ebc"


def _upd_dsu_port(self, _context):
    if runtime.dsu.running:
        runtime.dsu.start(self.dsu_port)  # restarts on the new port; an error is shown in the panels


def _upd_dolphin_folder(self, _context):
    runtime.manual_paths = dolphin_config.paths_from_folder(self.dolphin_folder) if self.dolphin_folder else None


class EBCPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    dsu_port: IntProperty(
        name="DSU port",
        description=(
            "UDP port of EBC's DSU pad on 127.0.0.1: Dolphin's hotkeys (screenshot, pause, frame advance, "
            "states) are bound to it. Configure Dolphin again after changing it"
        ),
        default=dsu.DEFAULT_PORT,
        min=1024,
        max=65535,
        update=_safe(_upd_dsu_port),
    )
    dolphin_folder: StringProperty(
        name="Dolphin folder",
        description=(
            "Dolphin's user folder (Config, ScreenShots, StateSaves inside), its configuration folder "
            "(Dolphin.ini inside, e.g. ~/.config/project-plus-dolphin), or the folder of Dolphin.exe. "
            "Empty = the folders of the Dolphin EBC connects to"
        ),
        default="",
        subtype="DIR_PATH",
        update=_safe(_upd_dolphin_folder),
    )
    detected_config: StringProperty(name="Detected configuration folder", default="")
    detected_data: StringProperty(name="Detected data folder", default="")

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "dsu_port")
        layout.prop(self, "dolphin_folder")
        paths = runtime.paths()
        col = layout.column(align=True)
        col.enabled = False
        if paths is not None:
            col.label(text=f"Configuration: {paths.config}")
            col.label(text=f"Data: {paths.data}  ({paths.source})")
        else:
            col.label(text="Dolphin folder unknown: connect once with Dolphin running, or pick it above")
        layout.operator("ebc.configure_dolphin", icon="PREFERENCES")


class _DefaultPrefs:
    dsu_port = dsu.DEFAULT_PORT
    dolphin_folder = ""
    detected_config = ""
    detected_data = ""


def prefs():
    """The extension preferences, or the defaults when EBC is not enabled as an add-on (tools)."""
    try:
        addon = bpy.context.preferences.addons.get(ADDON_ID)
        if addon is not None and addon.preferences is not None:
            return addon.preferences
    except Exception as exc:  # unregistered, or no context: the defaults  # noqa: BLE001
        print(f"[EBC] preferences unavailable: {exc!r}")
    return _DefaultPrefs()


def remember_detected(paths) -> None:
    """The running Dolphin's folders, kept in the preferences for Configure (Dolphin closed)."""
    runtime.dolphin_paths = paths
    p = prefs()
    if paths is not None and not isinstance(p, _DefaultPrefs):
        if p.detected_config != str(paths.config):
            p.detected_config = str(paths.config)
        if p.detected_data != str(paths.data):
            p.detected_data = str(paths.data)


def load_paths_from_prefs() -> None:
    """At register: the manual folder, else the last detected one (Dolphin may be closed now)."""
    p = prefs()
    runtime.manual_paths = dolphin_config.paths_from_folder(p.dolphin_folder) if p.dolphin_folder else None
    if runtime.dolphin_paths is None and p.detected_config and p.detected_data:
        runtime.dolphin_paths = dolphin_config.DolphinPaths(
            Path(p.detected_config), Path(p.detected_data), "last Dolphin EBC connected to"
        )


classes = (EBCTint, EBCCameraItem, EBCStagePreset, EBCSettings, EBCPreferences)


def register_props() -> None:
    bpy.types.Scene.ebc = PointerProperty(type=EBCSettings)  # type: ignore[attr-defined]
    try:
        load_paths_from_prefs()
    except Exception:  # never block the registration  # noqa: BLE001
        traceback.print_exc()


def unregister_props() -> None:
    del bpy.types.Scene.ebc  # type: ignore[attr-defined]
