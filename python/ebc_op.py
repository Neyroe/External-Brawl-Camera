import bpy
import dolphin_memory_engine as DME
from .ebc_functions import (
    init_globals,
    sync_blender_cam,
    sync_brawlCam_to_Blender,
    set_player_pos,
    get_current_frame,
    play_animation,
)

active_timers = {}


class menu_find_RSBE01(bpy.types.Operator):
    """Connect to Dolphin Emulator"""

    bl_idname = "wm.find_rsbe01"
    bl_label = "Connect to Dolphin"

    def execute(self, context):
        my_tool = context.scene.my_tool

        if not DME.is_hooked():
            DME.hook()

        if DME.is_hooked():
            print("Successfully hooked to Dolphin!")
            my_tool.game_found = True

            if not init_globals(context):
                self.report({"WARNING"}, "Warning: Camera or Origin not set in Blender.")

            return {"FINISHED"}
        else:
            print("Could not find Dolphin process.")
            self.report({"ERROR"}, "Could not find Dolphin. Is it running?")
            my_tool.game_found = False
            return {"CANCELLED"}


class menu_sync_camera(bpy.types.Operator):
    """A timer that consistently writes to Dolphins memory, Q to unsync"""

    bl_idname = "wm.sync_cam"
    bl_label = "Synchronize game"

    _timer = None

    def execute(self, context):
        wm = context.window_manager
        my_tool = context.scene.my_tool

        if self._timer is not None:
            self.cancel(context)

        my_tool.is_syncing = True
        self._timer = wm.event_timer_add(1 / 100, window=context.window)
        active_timers[self._timer] = self
        wm.modal_handler_add(self)

        print(f"Timer started. Total active timers: {len(active_timers)}")
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        wm = context.window_manager
        my_tool = context.scene.my_tool

        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            active_timers.pop(self._timer, None)
            self._timer = None

        my_tool.is_syncing = False
        print(f"Total active timers: {len(active_timers)}")

    def modal(self, context, event):
        if event.type in {"Q"} or not context.scene or not context.scene.my_tool or not self._timer:
            self.cancel(context)
            return {"CANCELLED"}

        if event.type == "TIMER":
            my_tool = context.scene.my_tool

            if get_current_frame() is not None:
                my_tool.frame_number = get_current_frame()
                if my_tool.target_frame == my_tool.frame_number:
                    my_tool.play_animation = True
            else:
                my_tool.game_found = False
                self.cancel(context)
                return {"CANCELLED"}

            if my_tool.is_sync_player:
                set_player_pos()

            if my_tool.camera_mod == "Default":
                sync_blender_cam(context)
            else:
                sync_brawlCam_to_Blender()

        return {"PASS_THROUGH"}


class menu_current_frame(bpy.types.Operator):
    bl_idname = "wm.frame"
    bl_label = "Current Frame"
    is_running = False

    def execute(self, context):
        wm = context.window_manager
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        return {"PASS_THROUGH"}

    def cancel(self, context):
        pass
