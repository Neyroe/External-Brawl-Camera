import bpy
from pathlib import Path
from .emc_common import RSBE01, CAM_TYPE
from .emc_functions import sync_blender_cam, sync_brawlCam_toBlender, set_player_pos, get_current_frame, change_FrontDepth_cam, change_BackDepth_cam

class menu_sync_camera(bpy.types.Operator):
    """A timer that consistently writes to Dolphins memory"""
    bl_idname = "wm.sync_cam"
    bl_label = "Sync Camera"

    def modal(self, context, event):
        if event.type in {'Q'}:
            self.cancel()
            return {'CANCELLED'}

        if event.type == 'TIMER':
            context.scene.my_tool.frame_number = get_current_frame() #Update current frame
            if context.scene.my_tool.reverse_sync:
                sync_brawlCam_toBlender()
            else: sync_blender_cam()
            if context.scene.my_tool.is_sync_player:
                set_player_pos() 

        return {'PASS_THROUGH'}

    def execute(self, context):
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.015, window=context.window) #0.017 1 frame
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)

class menu_current_frame(bpy.types.Operator):
    bl_idname = "wm.frame"
    bl_label = "Current Frame"
    is_running = False
    _timer = None

    def modal(self, context, event):
        if event.type == 'TIMER':
            context.scene.my_tool.frame_number = get_current_frame()
        return {'PASS_THROUGH'}

    def execute(self, context):
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)

class menu_DepthRadius(bpy.types.Operator):
    bl_idname = "wm.frame"
    bl_label = "DepthCam"
    is_running = False
    _timer = None

    bpy.types.Scene.frontSlider = bpy.props.FloatProperty(
        name="frontSlider",
        description="front",
        default = 1,
        min=1,
        max=1000,
        update = lambda self, context: change_FrontDepth_cam(self["frontSlider"])
    )

    bpy.types.Scene.backSlider = bpy.props.FloatProperty(
        name="backSlider",
        description="background",
        default=5000,
        min=1,
        max=5000,
        update = lambda self, context: change_BackDepth_cam(self["backSlider"])
    )
