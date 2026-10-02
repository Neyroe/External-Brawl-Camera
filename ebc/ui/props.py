"""Scene settings (``Scene.ebc``).

Properties are only ever read as attributes (``self.prop``): since Blender 5.0
registered properties are no longer reachable through ``self["prop"]``.

Every ``update=`` callback goes through ``_safe``: it calls the core, which is
a no-op while disconnected, and never lets an exception escape.
"""

import traceback

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
from bpy.types import PropertyGroup

from .. import runtime
from ..core import game as g
from ..core.addresses import PROFILES

SLOTS = (0, 1, 2, 3)


# Exceptions swallowed by update callbacks, kept for the smoke test.
callback_errors: list[str] = []


def _safe(fn):
    def update(self, context):
        try:
            fn(self, context)
        except Exception as exc:  # never break Blender's UI from an update callback
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
    # Driving the game camera from Blender only works with the debug camera lock.
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
    runtime.get_game().set_debug_menu(self.debug_menu)


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


def synced_camera(s):
    """The camera the sync sends (scene camera if listed, else the Rig camera), or None."""
    from . import sync  # sync imports this module

    cam = sync.effective_camera(s, s.id_data)
    return cam if cam is not None and cam.type == "CAMERA" else None


def keep_framing_ortho(cam, scene, distance: float | None = None) -> None:
    """PERSP -> ORTHO keeping roughly the framing: the ortho_scale showing what the perspective
    camera shows at ``distance`` (default: its view axis on the stage plane, else 100 units)."""
    from ..core import camera as cam_math
    from . import sync

    if distance is None:
        mw = [tuple(row) for row in cam.matrix_world]
        target = cam_math.plane_target(mw) or cam_math.axis_target(mw)
        eye = cam.matrix_world.translation
        distance = max(1.0, sum((eye[i] - target[i]) ** 2 for i in range(3)) ** 0.5)
    r = scene.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / max(1e-6, r.resolution_y * r.pixel_aspect_y)
    fov = sync.camera_vertical_fov(cam, scene)
    cam.data.ortho_scale = cam_math.ortho_scale_for_framing(distance, fov, aspect, cam.data.sensor_fit)


def _get_projection(self) -> int:
    try:
        cam = synced_camera(self)
        return 1 if cam is not None and cam.data.type == "ORTHO" else 0
    except Exception:
        return 0


def _set_projection(self, value: int) -> None:
    try:
        cam = synced_camera(self)
        if cam is None:
            return
        want = PROJECTIONS[value]
        if cam.data.type == want:
            return
        if want == "ORTHO" and cam.data.type == "PERSP":
            keep_framing_ortho(cam, self.id_data)
        cam.data.type = want
    except Exception as exc:  # never break Blender's UI from a property setter
        traceback.print_exc()
        callback_errors.append(f"_set_projection: {exc!r}")


def _upd_devserver_port(self, _context):
    runtime.devserver.set_port(self.devserver_port)


def tint_rgba(tint: "EBCTint") -> tuple[float, float, float, float]:
    r, gg, b = tuple(tint.color)
    return (r, gg, b, tint.alpha / 255.0)


def _upd_tint(self, _context):
    # ``self`` is the EBCTint; its path ("ebc.tint_p2") tells which slot it is.
    path = self.path_from_id()
    slot = int(path[-1]) - 1
    runtime.get_game().set_fighter_mode(slot, self.mode, tint_rgba(self))


# -- property groups --------------------------------------------------------------


class EBCTint(PropertyGroup):
    mode: EnumProperty(
        name="Mode",
        description="How this character is drawn (experimental)",
        items=[
            (g.FIGHTER_NORMAL, "Normal", "Leave the character as the game draws it"),
            (g.FIGHTER_COLOUR, "Colour", "Tint the character with the colour and alpha"),
            (g.FIGHTER_HIDDEN, "Hidden", "Do not draw the character (its effects stay visible)"),
        ],
        default=g.FIGHTER_NORMAL,
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
    keep_origin_on_plane: BoolProperty(
        name="Keep origin on the stage plane",
        description="Pin the origin to the stage plane (Blender Y = 0) and centre the camera view on it",
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
            (g.STAGE_HIDDEN, "Hidden", "Hide the stage: the void colour shows behind the fighters"),
            (g.STAGE_VISIBLE, "Visible", "Default stage"),
            (g.STAGE_COLLISION_ONLY, "Collision only", "Only the collision geometry"),
            (g.STAGE_VISIBLE_COLLISION, "Visible + collision", "Stage and collision geometry"),
        ],
        default=g.STAGE_VISIBLE,
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
    isolate_stage: StringProperty(default=g.STAGE_VISIBLE, options={"HIDDEN"})

    # -- Display -------------------------------------------------------------------
    hud: BoolProperty(name="HUD", description="Stocks, damage, timer", default=True, update=_safe(_upd_hud))
    debug_menu: BoolProperty(name="Debug menu", default=False, update=_safe(_upd_debug_menu))
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
                g.FREEZE_NATIVE,
                "Native",
                "Stop the game's match camera and write the whole camera: exact from the next frame, "
                "no shake, any aim, no Code Menu",
            ),
            (
                g.FREEZE_CODE_MENU,
                "Code Menu (legacy)",
                "Project+ Code Menu camera lock: target forced onto the stage plane, shakes on hits",
            ),
        ],
        default=g.FREEZE_NATIVE,
        update=_safe(_upd_freeze_method),
    )
    characters_display: EnumProperty(
        name="Characters",
        items=[
            (g.CHARACTERS_MODEL, "Model", "Default"),
            (g.CHARACTERS_MODEL_HURTBOXES, "Model + hurtboxes", "Model with hurtboxes"),
            (g.CHARACTERS_HURTBOXES, "Hurtboxes only", "Only the hurtboxes"),
            (g.CHARACTERS_HIDDEN, "Hidden", "Hide every character model"),
        ],
        default=g.CHARACTERS_MODEL,
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
            ("LIVE", "Live", "Record while the game runs at full speed (a missed frame is reported)"),
            ("STEP", "Frame by frame", "Pause, read, advance one frame, repeat (DevServer; exact on a replay)"),
        ],
        default="LIVE",
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
        return getattr(self, f"tint_p{slot + 1}")

    def fighter_modes(self) -> list[str]:
        return [self.tint(slot).mode for slot in SLOTS]

    def player(self, slot: int):
        return getattr(self, f"player_{slot + 1}")


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
        if tint.mode != g.FIGHTER_NORMAL:
            _safe(_upd_tint)(tint, None)


classes = (EBCTint, EBCCameraItem, EBCSettings)


def register_props() -> None:
    bpy.types.Scene.ebc = PointerProperty(type=EBCSettings)  # type: ignore[attr-defined]


def unregister_props() -> None:
    del bpy.types.Scene.ebc  # type: ignore[attr-defined]
