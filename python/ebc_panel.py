import bpy
from bpy.props import (FloatVectorProperty, IntProperty, StringProperty, BoolProperty, PointerProperty)
from bpy.types import (Panel, PropertyGroup)
from .ebc_op import menu_sync_camera, menu_current_frame
from .ebc_functions import *

class control_properties(PropertyGroup):
    # To be implemented.
    def update_panel(self, context):
    # Use the tag_redraw method to mark the panel for redrawing
        context.area.tag_redraw()

    reverse_sync: BoolProperty(
    name="Brawl Camera over blender",
    description="Blender camera move as Brawl camera",
    default=False
    )

    lock_camera_to_view_toggle:BoolProperty(
    name="Lock camera to view",    
    description="When looking in the camera (press numpad0 by default), you will be center on origin + origin will stay at Z = 0",
    default = False,   
    )

    lock_camera_z_rotation:BoolProperty(
    name="Lock camera Z rotation",
    description="If you want to have a camera wih is always looking forward, toggle On",
    default = False,   
    )

    is_sync_player: BoolProperty(
        name="Player Positions",
        description="Sync player position from Pm to blender",
        default=False
        )

    frame_number: IntProperty(
        default=0,
    )
    
    front_depth_slider: IntProperty(
        name="Front depth slider",
        description="Do not render anything ahead the distance you choose",  
        default=1,
        min=1,
        max=1000,
        update=lambda self, context: update_FrontDepth_cam(self["front_depth_slider"])
    )

    back_depth_slider: IntProperty(
        name="Back depth slider",
        description="Do not render anything behind the distance you choose",
        default=5000,
        min=1,
        max=5000,
        update=lambda self, context: update_BackDepth_cam(self["back_depth_slider"])
    )

    hurtbox_display: bpy.props.EnumProperty(
        items=[
            ('Default', 'Hurtbox OFF', 'Only Display 3D model'),
            ('Hurtbox On', 'Hurtbox + 3D model ON', 'Display 3D model with hurtbox active'),
            ('Hurtbox On & 3D model OFF', 'Only hurtbox', 'Only Display hurtbox without 3D model'),
            ('No character', 'Hurtbox & 3D model OFF', 'Hide character, WIP (MK)'),
        ],
        name= "Hurtbox Display",
        default='Default',
        update = lambda self, context: change_character_debug_mod(self["hurtbox_display"])
    )

    stage_collision:bpy.props.EnumProperty(
        items=[
            ('Default', 'Default Stage', 'Only Display 3D model'),
            ('Collision On', 'Collision + 3D model', 'Display 3D model with Collision'),
            ('Collision On & 3D model OFF', 'Only Collision', 'Only Display Collision without 3D model'),
            ('Global Stage OFF', 'Stage OFF', 'Hide Stage'),
        ],
        name= "Stage Collision Display",
        default='Default',
        update = lambda self, context: change_stage_debug_mod(self["stage_collision"])
    )
    
    debug_menu: BoolProperty(
        name="Project M Debug menu",
        description="",
        default=False,
        update = lambda self, context: enable_debug_mod(self["debug_menu"])
        )

    HUD:BoolProperty(
        name="Display HUD",
        description="Timer, stock, %",
        default=True,
        update = lambda self, context: enable_HUD(self["HUD"])
        )

    draw_di:BoolProperty(
        name="Draw DI",
        description="",
        default=False,   
        update = lambda self, context: draw_di(self["draw_di"])
        )
        
    music_slider: IntProperty(
        name="Music slider",
        description="music volume",
        default=100,
        min=0,
        max=100,
        update=lambda self, context: update_music_volume(self["music_slider"])
    )

    sound_effect_slider: IntProperty(
        name="sound_effect_slider",
        description="background",
        default=100,
        min=0,
        max=100,
        update=lambda self, context: update_sound_effect_volume(self["sound_effect_slider"])
    )

    shadow_X_orientation_slider: IntProperty(
        name="Shadow Y orientation",
        description="Change shadow orientation",
        default=90,
        min=0,
        max=180,
        update=lambda self, context: update_shadow_X_orientation(self["shadow_X_orientation_slider"])
    )
    
    shadow_Y_orientation_slider: IntProperty(
        name="Shadow Y orientation",
        description="Change shadow orientation",
        default=0,
        min=-180,
        max=180,
        update=lambda self, context: update_shadow_Y_orientation(self["shadow_Y_orientation_slider"])
    )

    character_shadow:BoolProperty(
    name="Character Shadow",
    description="RESET MATCH = RESET SHADOW",
    default = True,   
    update = lambda self, context: enable_character_shadow(self["character_shadow"])
    )

    color_background_toggle:BoolProperty(
    name="Character Shadow",
    description="RESET MATCH = RESET SHADOW",
    default = False,   
    update = lambda self, context: enable_custom_color_background(self["color_background_toggle"])
    )
    
    color_background_picker: FloatVectorProperty(
        name="Background color",
        description="Choose a color",
        subtype='COLOR',
        min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        size=4,
        update= lambda self, context: update_custom_color_background(self["color_background_toggle"], self["color_background_picker"])
    )

class ebc_control_panel(Panel):
    bl_label = 'Sync Brawl Camera'
    bl_idname = 'OBJECT_PT_external_brawl_camera'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Blender Camera to Brawl'
    bl_context = 'objectmode'
    panel_timer = None
    @classmethod
    def poll(self, context):
        return context.object is not None
        
    def draw(self, context):
        layout = self.layout
        mytool = context.scene.my_tool

        # Sync Camera
        ebc_box = layout.box()
        button_sync = ebc_box.row()
        button_sync.alignment = 'Expand'.upper()
        button_sync.scale_y = 2
        button_sync.operator('wm.sync_cam', icon_value=71)
        # Frame Display
        frame_row = ebc_box.row()
        frame_row.alignment = 'CENTER'
        frame_row.prop(mytool, 'is_sync_player')
        frame_row.label(text=f'Frame: {mytool.frame_number}')

        # other toggle
        frame_row = ebc_box.column()
        ebc_toggle = frame_row.row()
        ebc_toggle.alignment = 'CENTER'
        ebc_toggle.prop(mytool, 'reverse_sync')
        ebc_toggle.prop(mytool, 'lock_camera_to_view_toggle')
        ebc_toggle.prop(mytool, 'lock_camera_z_rotation')


        # Camera Depth Slider
        debug_mod_box = layout.box()

        debug_mod_title = debug_mod_box.row()
        debug_mod_title.alignment = 'CENTER'
        debug_mod_title.label(text="Debug mod Feature", icon='VIEW_CAMERA')
        
        slider_cam = debug_mod_box.column()
        slider_cam.prop(mytool, 'front_depth_slider', slider=True, text="Front Depth")
        slider_cam.prop(mytool, 'back_depth_slider', slider=True, text="Background Depth")
        
        #Debug option
        debug_options = debug_mod_box.column()
        debug_options.prop(mytool, 'hurtbox_display', text="Hurtbox Display")
        debug_options.prop(mytool, 'stage_collision', text="Stage Collision")

        debug_options_plus = debug_mod_box.row()
        debug_options_plus.alignment = 'CENTER'
        debug_options_plus.prop(mytool, 'HUD', text="HUD")
        debug_options_plus.prop(mytool, 'draw_di', text="Draw DI")
        debug_options_plus.prop(mytool, 'debug_menu', text="Debug Menu")

        # Music and Sound effect
        sound_box = layout.box()

        sound_title = sound_box.row()
        sound_title.alignment = 'CENTER'
        sound_title.label(text="Volume", icon='PLAY_SOUND')

        slider_volume = sound_box.column()
        slider_volume.prop(mytool, 'music_slider', slider=True, text="Music Volume")
        slider_volume.prop(mytool, 'sound_effect_slider', slider=True, text="Sound effect Volume")

        # Shadow Orientation and display
        shadow_box = layout.box()
        
        shadow_title = shadow_box.row()
        shadow_title.alignment = 'CENTER'
        shadow_title.label(text="Charater Shadow", icon='COMMUNITY')

        shadow_toggle = shadow_box.row()
        shadow_toggle.alignment = 'CENTER'
        shadow_toggle.prop(mytool, 'character_shadow', text="Enable Character shadow")

        shadow_slider = shadow_box.column()
        shadow_slider.prop(mytool, 'shadow_X_orientation_slider', slider=True, text="Shadow X orientation")
        shadow_slider.prop(mytool, 'shadow_Y_orientation_slider', slider=True, text="Shadow Y orientation")

        #Stage Green screen
        color_box = layout.box()

        color_title = color_box.row()
        color_title.label(text="Custom Background Color", icon='COLOR')
        color_title.alignment = 'CENTER'

        colot_toggle = color_box.row()
        colot_toggle.alignment = 'CENTER'
        colot_toggle.prop(mytool, 'color_background_toggle', text="Enable custom background color")
        
        color_picker = color_box.column()
        color_picker.prop(mytool, "color_background_picker")
        color_picker.prop(mytool, "color_background_picker", text="R", index=0)
        color_picker.prop(mytool, "color_background_picker", text="G", index=1)
        color_picker.prop(mytool, "color_background_picker", text="B", index=2)
        color_picker.prop(mytool, "color_background_picker", text="A", index=3)


classes = (
    control_properties,
    ebc_control_panel,
    menu_sync_camera,
    menu_current_frame,
    )