import bpy
from pathlib import Path
from .ebc_functions import sync_blender_cam, sync_brawlCam_to_Blender, set_player_pos, get_current_frame, init_globals

class menu_sync_camera(bpy.types.Operator):
    """A timer that consistently writes to Dolphins memory"""
    bl_idname = "wm.sync_cam"
    bl_label = "Sync Camera"

    def modal(self, context, event):
        if event.type in {'Q'}:
            self.cancel()
            return {'CANCELLED'}

        if event.type == 'TIMER':
            lock_cam_view = context.scene.my_tool.lock_camera_to_view_toggle
            lock_rot_cam_z = context.scene.my_tool.lock_camera_z_rotation
            context.scene.my_tool.frame_number = get_current_frame() #Update current frame
            if context.scene.my_tool.reverse_sync:
                sync_brawlCam_to_Blender()
            else: 
                sync_blender_cam(lock_cam_view,lock_rot_cam_z)
            if context.scene.my_tool.is_sync_player:
                set_player_pos() 

        return {'PASS_THROUGH'}

    def execute(self, context):
        wm = context.window_manager
        self._timer = wm.event_timer_add(1/120, window=context.window)
        init_globals(context)
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
        self._timer = wm.event_timer_add(1/90, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)