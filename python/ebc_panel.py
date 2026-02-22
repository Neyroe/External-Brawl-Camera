import bpy
from bpy.props import FloatVectorProperty, IntProperty, BoolProperty, PointerProperty, EnumProperty, FloatProperty
from bpy.types import Panel, PropertyGroup
from .ebc_op import menu_find_RSBE01, menu_sync_camera, menu_current_frame
from .ebc_functions import *


class control_properties(PropertyGroup):
    # To be implemented.
    def update_panel(self, context):
        # Use the tag_redraw method to mark the panel for redrawing
        context.area.tag_redraw()

    def sync_camera_properties(self):
        """Synchronize the sliders with the current camera properties."""
        if self.linked_object and self.linked_object.type == "CAMERA":
            print("Camera slider have been update")
            cam_data = self.linked_object.data
            self.fov = math.degrees(cam_data.angle)
            self.front_depth_slider = cam_data.clip_start
            self.back_depth_slider = cam_data.clip_end

    game_found: BoolProperty(
        name="Game Found", description="Indicates if the game has been found in memory", default=True
    )

    is_syncing: BoolProperty(
        name="sync",
        description="Indicates if blender communicate at least 60 times per second with dolphin",
        default=False,
    )

    camera_mod: EnumProperty(
        items=[
            ("Default", "Blender camera over brawl", "Transform position of blender camera overwrite brawl camera"),
            ("Brawl Camera", "Brawl Camera", "Show the current position of brawl camera in your scene"),
        ],
        name="",
        default="Default",
        update=lambda self, context: update_camera_mod(self["camera_mod"]),
    )

    lock_camera_to_view_toggle: BoolProperty(
        name="Lock camera to view",
        description="When looking in the camera (press numpad0 by default), you will be center on origin + origin will stay at Z = 0",
        default=False,
    )

    lock_camera_z_rotation: BoolProperty(
        name="Lock camera Z rotation",
        description="If you want to have a camera wih is always looking forward, toggle On",
        default=False,
    )

    is_sync_player: BoolProperty(
        name="Player Positions", description="Sync player position from Pm to blender", default=False
    )

    frame_number: IntProperty(
        default=0,
    )

    active_camera: PointerProperty(name="Linked Camera", description="Select an camera", type=bpy.types.Object)

    active_origin: PointerProperty(
        name="Linked Origin point",
        description="This is the point where the camera is poiting, the rotation camera doesn't matter in brawl",
        type=bpy.types.Object,
    )

    custom_fov_toggle: BoolProperty(
        name="",
        description="Custom the Camera FOV, camera might have a 'shake' effect if not update properly",
        default=False,
    )

    # fov_slider= FloatProperty(
    #     name="FOV slider",
    #     default=52.36,
    #     min=1,
    #     max=313,
    # )

    front_depth_slider: IntProperty(
        name="Front depth slider",
        description="Do not render anything ahead the distance you choose",
        default=1,
        min=1,
        max=1000,
        update=lambda self, context: update_FrontDepth_cam(self["front_depth_slider"]),
    )

    back_depth_slider: IntProperty(
        name="Back depth slider",
        description="Do not render anything behind the distance you choose",
        default=5000,
        min=1,
        max=5000,
        update=lambda self, context: update_BackDepth_cam(self["back_depth_slider"]),
    )

    hurtbox_display: EnumProperty(
        items=[
            ("Default", "Default", "Only Display 3D model"),
            ("Hurtbox On", "Hurtbox + 3D model ON", "Display 3D model with hurtbox active"),
            ("Hurtbox On & 3D model OFF", "Only hurtbox", "Only Display hurtbox without 3D model"),
            ("Hurtbox & 3D model OFF", "No character", "Hide character, WIP (MK), should be completed with shadow off"),
        ],
        name="",
        default="Default",
        update=lambda self, context: change_character_debug_mod(self["hurtbox_display"]),
    )

    stage_collision: EnumProperty(
        items=[
            ("Default", "Default Stage", "Only Display 3D model"),
            ("Collision On", "Collision + 3D model", "Display 3D model with Collision"),
            ("Collision On & 3D model OFF", "Only Collision", "Only Display Collision without 3D model"),
            ("Global Stage OFF", "Stage OFF", "Hide Stage"),
        ],
        name="",
        default="Default",
        update=lambda self, context: change_stage_debug_mod(self["stage_collision"]),
    )

    debug_menu: BoolProperty(
        name="Project M Debug menu",
        description="",
        default=False,
        update=lambda self, context: enable_debug_mod(self["debug_menu"]),
    )

    HUD: BoolProperty(
        name="Display HUD",
        description="Timer, stock, %, ...",
        default=True,
        update=lambda self, context: enable_HUD(self["HUD"]),
    )

    draw_di: BoolProperty(
        name="Draw DI", description="", default=False, update=lambda self, context: draw_di(self["draw_di"])
    )

    lock_camera: BoolProperty(
        name="Lock Camera", description="", default=False, update=lambda self, context: lock_camera(self["lock_camera"])
    )

    music_slider: IntProperty(
        name="Music slider",
        description="music volume",
        default=100,
        min=0,
        max=100,
        update=lambda self, context: update_music_volume(self["music_slider"]),
    )

    sound_effect_slider: IntProperty(
        name="sound_effect_slider",
        description="background",
        default=100,
        min=0,
        max=100,
        update=lambda self, context: update_sound_effect_volume(self["sound_effect_slider"]),
    )

    shadow_X_orientation_slider: IntProperty(
        name="Shadow Y orientation",
        description="Change shadow orientation",
        default=90,
        min=0,
        max=180,
        update=lambda self, context: update_shadow_X_orientation(self["shadow_X_orientation_slider"]),
    )

    shadow_Y_orientation_slider: IntProperty(
        name="Shadow Y orientation",
        description="Change shadow orientation",
        default=0,
        min=-180,
        max=180,
        update=lambda self, context: update_shadow_Y_orientation(self["shadow_Y_orientation_slider"]),
    )

    character_shadow: BoolProperty(
        name="Character Shadow",
        description="RESET MATCH = RESET SHADOW",
        default=True,
        update=lambda self, context: enable_character_shadow(self["character_shadow"]),
    )

    ###################### GREEN SCREEN ############################################
    color_background_toggle: BoolProperty(
        name="Toggle true green screen",
        default=False,
        update=lambda self, context: enable_custom_color_background(self["color_background_toggle"]),
    )

    color_background_picker: FloatVectorProperty(
        name="Green screen color",
        description="Choose a color",
        subtype="COLOR",
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        size=4,
        update=lambda self, context: update_custom_color_background(
            self["color_background_toggle"], self["color_background_picker"]
        ),
    )

    # GREEN SCREEN BACKGROUND STAGE ############################################

    color_background_stage_toggle: BoolProperty(
        name="Toggle custom background color",
        description="By default the 'void' color is black",
        default=False,
        update=lambda self, context: update_custom_color_background_stage(
            self.color_background_stage_toggle, self.color_stage_background_picker
        ),
    )

    color_stage_background_picker: FloatVectorProperty(
        name="Stage Background color",
        description="Choose a color",
        subtype="COLOR",
        min=0.0,
        max=1.0,
        default=(0, 0, 0, 1),
        size=4,
        update=lambda self, context: update_custom_color_background_stage(
            self.color_background_stage_toggle, self.color_stage_background_picker
        ),
    )

    ###################### GREEN SCREEN ############################################

    target_frame: IntProperty(
        name="Target Frame", description="Frame on which the animation will start", default=1, min=1
    )

    play_animation: BoolProperty(
        name="",
        description="Trigger the animation when the target frame is reached",
        default=False,
        update=lambda self, context: play_animation(self["play_animation"]),
    )


class display_box_panel(PropertyGroup):
    show_camera_panel: BoolProperty(
        name="Show Camera Feature", description="Toggle the visibility of the Camera Feature section", default=True
    )
    show_debug_mod_feature_panel: BoolProperty(
        name="Show Debug Mod Feature",
        description="Toggle the visibility of the Debug Mod Feature section",
        default=True,
    )
    show_music_panel: BoolProperty(
        name="Show Music & Sound section",
        description="Toggle the visibility of the Music & Sound section",
        default=True,
    )
    show_shadow_panel: BoolProperty(
        name="Show Shadow section", description="Toggle the visibility of the Shadow section", default=True
    )
    show_green_screen_panel: BoolProperty(
        name="Show Green Screen section", description="Toggle the visibility of the Green Screen section", default=True
    )

    show_animation_panel: BoolProperty(
        name="Show Animation section", description="Toggle the visibility of the Animation section", default=True
    )


class ebc_control_panel(Panel):
    bl_label = "Sync Brawl Camera"
    bl_idname = "OBJECT_PT_external_brawl_camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender Camera to Brawl"
    bl_context = "objectmode"
    panel_timer = None

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def draw(self, context):
        layout = self.layout
        my_tool = context.scene.my_tool
        display_box_panel = context.scene.my_tool_display

        base_box = layout.box()
        button_find_game = base_box.row()
        button_find_game.operator("wm.find_rsbe01", text="Find RSBE01", icon="QUESTION")

        ebc_box = layout.box()
        ebc_box.enabled = my_tool.game_found

        if my_tool.game_found:
            button_find_game.label(text="Game Found", icon="CHECKMARK")
        else:
            button_find_game.label(text="Game Not Found", icon="ERROR")

        ########## MENU SYNC ##########
        # Désactiver les boutons si le jeu n'est pas trouvé
        sync_box = ebc_box.box()
        button_sync = sync_box.row()
        button_sync.alignment = "Expand".upper()
        button_sync.scale_y = 2
        button_sync.operator("wm.sync_cam", text="Synchronize Game", icon_value=71)
        # button_sync.enabled = not my_tool.is_syncing

        # Frame Display
        frame_row = sync_box.row()
        frame_row.alignment = "CENTER"
        frame_row.prop(my_tool, "is_sync_player")
        frame_row.label(text=f"Frame: {my_tool.frame_number}")

        ########## MENU CAMERA ##########
        camera_box = ebc_box.box()
        icon = "TRIA_DOWN" if display_box_panel.show_camera_panel else "TRIA_RIGHT"

        # display menu
        camera_title = camera_box.row()
        camera_title.prop(display_box_panel, "show_camera_panel", text="", icon=icon)
        camera_title.label(text="CAMERA", icon="VIEW_CAMERA")
        camera_box.prop(my_tool, "camera_mod", text="Camera mode")

        if display_box_panel.show_camera_panel:
            camera_box.prop(my_tool, "active_camera", text="Active Camera")
            camera_box.prop(my_tool, "active_origin", text="Active Origin")

            frame_row = camera_box.column()
            camera_toggle = frame_row.row()
            camera_toggle.alignment = "CENTER"
            camera_toggle.prop(my_tool, "lock_camera_to_view_toggle")
            camera_toggle.prop(my_tool, "lock_camera_z_rotation")
            # Camera Depth Slider
            if isinstance(my_tool.active_camera, bpy.types.Object) and my_tool.active_camera.type == "CAMERA":
                cam_data = my_tool.active_camera.data

                # Propriétés de la caméra (FOV, Clip Start, Clip End)
                fov_cam = camera_box.row()
                fov_cam.prop(my_tool, "custom_fov_toggle", text="")
                fov_cam.prop(cam_data, "angle", text="FOV")  # or lens
                fov_cam.operator("object.reset_fov", text="", icon="FILE_REFRESH")
            else:
                camera_box.label(text="Select a camera to see its properties", icon="INFO")

            # fov_cam = camera_box.row()
            # fov_cam.prop(my_tool, 'custom_fov_toggle', text="")
            # fov_cam.prop(my_tool, 'fov_slider', slider=True, text="Custom FOV")

            slider_depth = camera_box.column()
            slider_depth.prop(my_tool, "front_depth_slider", slider=True, text="Front Depth")
            slider_depth.prop(my_tool, "back_depth_slider", slider=True, text="Background Depth")

        ########## MENU DEBUG MOD FEATURE ##########
        debug_mod_box = ebc_box.box()
        icon = "TRIA_DOWN" if display_box_panel.show_debug_mod_feature_panel else "TRIA_RIGHT"

        # display menu
        debug_mod_title = debug_mod_box.row()
        debug_mod_title.prop(display_box_panel, "show_debug_mod_feature_panel", text="", icon=icon)
        debug_mod_title.label(text="Debug Mod Feature", icon="SEQ_CHROMA_SCOPE")

        if display_box_panel.show_debug_mod_feature_panel:
            # Debug option
            debug_options = debug_mod_box.column()
            debug_options.prop(my_tool, "hurtbox_display", text="Hurtbox Display")
            debug_options.prop(my_tool, "stage_collision", text="Stage Collision")

            debug_options_plus = debug_mod_box.row()
            debug_options_plus.alignment = "CENTER"
            debug_options_plus.prop(my_tool, "HUD", text="HUD")
            debug_options_plus.prop(my_tool, "draw_di", text="Draw DI")
            debug_options_plus.prop(my_tool, "lock_camera", text="Lock The Camera")
            debug_options_plus.prop(my_tool, "debug_menu", text="Debug Menu")

        ########## MENU MUSIC & SOUND ##########
        sound_box = ebc_box.box()
        icon = "TRIA_DOWN" if display_box_panel.show_music_panel else "TRIA_RIGHT"

        # display menu
        sound_title = sound_box.row()
        sound_title.prop(display_box_panel, "show_music_panel", text="", icon=icon)
        sound_title.label(text="Music & Sound", icon="PLAY_SOUND")

        if display_box_panel.show_music_panel:
            slider_volume = sound_box.column()
            slider_volume.prop(my_tool, "music_slider", slider=True, text="Music Volume")
            slider_volume.prop(my_tool, "sound_effect_slider", slider=True, text="Sound Effect Volume")

        ########## MENU SHADOW ##########
        shadow_box = ebc_box.box()
        icon = "TRIA_DOWN" if display_box_panel.show_shadow_panel else "TRIA_RIGHT"

        # display menu
        shadow_title = shadow_box.row()
        shadow_title.prop(display_box_panel, "show_shadow_panel", text="", icon=icon)
        shadow_title.label(text="Character Shadow", icon="COMMUNITY")

        if display_box_panel.show_shadow_panel:
            shadow_toggle = shadow_box.row()
            shadow_toggle.alignment = "CENTER"
            shadow_toggle.prop(my_tool, "character_shadow", text="Enable Character shadow")

            shadow_slider = shadow_box.column()
            shadow_slider.prop(my_tool, "shadow_X_orientation_slider", slider=True, text="Shadow X orientation")
            shadow_slider.prop(my_tool, "shadow_Y_orientation_slider", slider=True, text="Shadow Y orientation")

        ########## MENU GREEN SCREEN ##########
        icon = "TRIA_DOWN" if display_box_panel.show_green_screen_panel else "TRIA_RIGHT"
        # display menu
        color_box = ebc_box.box()
        color_title = color_box.row()
        color_title.prop(display_box_panel, "show_green_screen_panel", text="", icon=icon)
        color_title.label(text="Custom Background Color", icon="COLOR")

        if display_box_panel.show_green_screen_panel:
            # Toggle et picker sur la même ligne
            color_row = color_box.row()
            color_row.prop(my_tool, "color_background_toggle", text="GREEN SCREEN")
            color_row.prop(my_tool, "color_background_picker", text="")

            # Composantes RGBA en dessous
            color_picker = color_box.column()
            color_picker.prop(my_tool, "color_background_picker", text="R", index=0)
            color_picker.prop(my_tool, "color_background_picker", text="G", index=1)
            color_picker.prop(my_tool, "color_background_picker", text="B", index=2)
            color_picker.prop(my_tool, "color_background_picker", text="A", index=3)

            color_row2 = color_box.row()
            color_row2.prop(my_tool, "color_background_stage_toggle", text="STAGE BACKGROUND COLOR")
            color_row2.prop(my_tool, "color_stage_background_picker", text="")

            color_picker2 = color_box.column()
            color_picker2.prop(my_tool, "color_stage_background_picker", text="R", index=0)
            color_picker2.prop(my_tool, "color_stage_background_picker", text="G", index=1)
            color_picker2.prop(my_tool, "color_stage_background_picker", text="B", index=2)
            color_picker2.prop(my_tool, "color_stage_background_picker", text="A", index=3)

        ########## MENU ANIMATION ##########
        icon = "TRIA_DOWN" if display_box_panel.show_animation_panel else "TRIA_RIGHT"
        # display menu
        animation_box = ebc_box.box()
        animation_title = animation_box.row()
        animation_title.prop(display_box_panel, "show_animation_panel", text="", icon=icon)
        animation_title.label(text="ANIMATION", icon="SEQUENCE")

        if display_box_panel.show_animation_panel:
            animation_row = animation_box.row()
            animation_row.prop(my_tool, "target_frame")
            if my_tool.play_animation:
                animation_row.prop(my_tool, "play_animation", icon="REW")
            else:
                animation_row.prop(my_tool, "play_animation", icon="PLAY")


class reset_FOV_Operator(bpy.types.Operator):
    bl_idname = "object.reset_fov"
    bl_label = "Reset FOV to Default"

    def execute(self, context):
        cam = context.scene.my_tool.active_camera
        cam.data.lens = 55.9808  # angle = 30
        self.report({"INFO"}, "FOV reset")
        return {"FINISHED"}


classes = (
    control_properties,
    display_box_panel,
    ebc_control_panel,
    menu_find_RSBE01,
    menu_sync_camera,
    menu_current_frame,
    reset_FOV_Operator,
)
