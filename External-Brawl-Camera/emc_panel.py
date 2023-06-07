import bpy
from bpy.props import (IntProperty, StringProperty, BoolProperty, PointerProperty)
from bpy.types import (Panel, PropertyGroup)
from .emc_op import menu_sync_camera, menu_current_frame, menu_DepthRadius

class control_properties(PropertyGroup):

    reverse_sync: BoolProperty(
    name="Brawl Camera over blender",
    description="",
    default=False
    )

    is_sync_player: BoolProperty(
        name="Player Positions",
        description="Sync player position from Pm to blender",
        default=False
        )

    frame_number: IntProperty(
        default=0,
    )

# To be implemented.
def update_panel(self, context):
    # Use the tag_redraw method to mark the panel for redrawing
    context.area.tag_redraw()

class ebc_control_panel(Panel):
    bl_label = 'Sync Brawl Camera'
    bl_idname = 'OBJECT_PT_external_melee_camera'
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
        cam_box = layout.box()
        cam_row1 = cam_box.row()
        cam_row1.scale_y = 2
        cam_row1.operator('wm.sync_cam', icon_value=71)
        cam_row2 = cam_box.row()
        cam_row2.alignment = 'Center'.upper()
        cam_row2.prop(mytool, 'reverse_sync')
        cam_row2.prop(mytool, 'is_sync_player')

        layout.prop(context.scene, "frontSlider", slider=True, text="Front Depth")
        layout.prop(context.scene, "backSlider", slider=True, text="Background Depth")
        
        layout.separator()
        # Frame Display
        cam_box.alignment = 'Expand'.upper()
        frame_row = cam_box.row()
        frame_row.alignment = 'Center'.upper()
        frame_row.label(text=f'Frame: {context.scene.my_tool.frame_number}')

classes = (
    control_properties,
    ebc_control_panel,
    menu_sync_camera,
    menu_current_frame,
    menu_DepthRadius,
    )