import bpy
import math
import struct
from .ebc_common import *
import dolphin_memory_engine as DME


def init_globals(context):
    global BLorg, BLCam, my_tool
    my_tool = context.scene.my_tool
    # if my_tool.active_camera is not None else bpy.data.objects['Camera']
    BLCam = my_tool.active_camera
    BLorg = my_tool.active_origin
    return BLorg is not None and BLCam is not None


def lock_camera_to_view():
    # Lock the camera in blender windows to only look forward the Origin point (org)
    BLorg.matrix_world.translation.y = 0  # Origin can not moove arround this axe (axis Z for smash brawl)
    BLorg.location[1] = 0  # make sure that Origin stay straight where camera is looking
    BLorg.location[2] = 0
    for area in bpy.context.screen.areas:  # Make the org pos always at 0 and lock the camera arround a correct axis
        if area.type == "VIEW_3D":
            rv3d = area.spaces[0].region_3d
            if rv3d.view_perspective == "CAMERA":
                rv3d.view_location.y = BLorg.matrix_world.translation.y


def sync_blender_z_cam_rotation(cam_rot):
    # Camera will always look forward if you want to do so.
    DME.write_bytes(RSBE01 + CAM_ROT_Z_ADRR, struct.pack(">f", cam_rot))
    # World or local might change how camera interact


def sync_blender_cam(context):
    if not init_globals(context):
        return

    BLorg_loc = BLorg.matrix_world.translation.xzy
    BLorg_loc.x *= -1

    BLCam_loc = BLCam.matrix_world.translation.xzy
    BLCam_loc.x *= -1
    if my_tool.custom_fov_toggle:
        update_FOV_cam(BLCam.data.angle)
    if my_tool.lock_camera_to_view_toggle:
        lock_camera_to_view()
    if my_tool.lock_camera_z_rotation:
        sync_blender_z_cam_rotation(BLCam.rotation_euler.y)

    if check_cam_type():
        Cam_IG_Data = [BLorg_loc, BLCam_loc]
        mat_bytes = b""
        for r in Cam_IG_Data:
            for c in r:
                mat_bytes += struct.pack(">f", c)
        DME.write_bytes(RSBE01 + CAM_IG_ORG_ADRR, mat_bytes)

    else:  # camera on PAUSE WIP
        mat_bytes = b""
        for c in BLorg_loc:
            mat_bytes += struct.pack(">f", c)
        DME.write_bytes(RSBE01 + CAM_PAUSE_ORG_ADRR, mat_bytes)

        # Angle from Origin to camera, in a good world camera is always looking to org
        DME.write_bytes(
            RSBE01 + CAM_PAUSE_ANGLE_ADRR, struct.pack(">f", (-BLCam.matrix_world.to_euler().x - math.pi / 2))
        )  # X radiant
        DME.write_bytes(
            RSBE01 + CAM_PAUSE_ANGLE_ADRR + 0x4, struct.pack(">f", BLCam.matrix_world.to_euler().z)
        )  # Y radiant

        Cam_Position_To_Org = math.sqrt(
            (BLCam_loc.x - BLorg_loc.x) ** 2 + (BLCam_loc.y - BLorg_loc.y) ** 2 + (BLCam_loc.z - BLorg_loc.z) ** 2
        )
        DME.write_bytes(
            RSBE01 + DISTANCE_CAM_ORG_ADRR, struct.pack(">f", Cam_Position_To_Org)
        )  # Distance camera to origin


def check_cam_type():
    current_type = DME.read_byte(RSBE01 + CAM_TYPE)  # return int
    return True if current_type == 0 else False
    # Camera on Match 0x5B6D80
    # Camera on Pause 0x6636C8


def set_player_pos():
    liste_players = [0, 0, 0, 0]
    liste_players[0] = bpy.data.objects["PLAYER_1"]
    liste_players[1] = bpy.data.objects["PLAYER_2"]
    liste_players[2] = bpy.data.objects["PLAYER_3"]
    liste_players[3] = bpy.data.objects["PLAYER_4"]

    """ #Don't find player in a good order :/
    Player_Collection = bpy.data.collections.get("PLAYERS")
    for objet in Player_Collection.objects:#Add every player in list
        liste_players.append(objet)"""

    for r in range(len(liste_players)):
        if (
            DME.read_byte(RSBE01 + list_playerID[r]) < 50 and DME.read_byte(RSBE01 + list_playerID[r]) > 0
        ):  # be sure if they are a player to display
            pos = find_matrix_position(r)
            if pos is not None:
                liste_players[r].location = pos
                liste_players[r].location.x *= -1
                liste_players[r].hide_set(False)  # Be sure to not hide on scene
        else:
            liste_players[r].hide_set(True)  # Hide on scene nothing more to do


def sync_player_control(addr):
    anim_byte = 0 if bpy.context.screen.is_animation_playing else 1
    buf = struct.pack(">b", anim_byte)
    DME.write_bytes(addr, buf)


def sync_brawlCam_to_Blender():
    # Define Camera and Pivot point
    BLorg = bpy.data.objects["Origin"]
    BLcam = bpy.data.objects["Camera"]

    brawlOrg_loc = [0, 0, 0]
    brawlCam_loc = [0, 0, 0]

    for i in range(3):
        byteBrawlOrg_loc = DME.read_bytes(RSBE01 + CAM_IG_ORG_ADRR + (i * 0x4), 4)
        brawlOrg_loc[i] = struct.unpack(">f", byteBrawlOrg_loc)[0]

        byteBrawlCam_loc = DME.read_bytes((RSBE01 + CAM_IG_POSITION_ADRR + (i * 0x4)), 4)
        brawlCam_loc[i] = struct.unpack(">f", byteBrawlCam_loc)[0]

    brawlCam_loc[0] *= -1
    brawlOrg_loc[0] *= -1
    BLcam.matrix_world.translation.xzy = brawlCam_loc
    BLorg.matrix_world.translation.xzy = brawlOrg_loc


def get_current_frame():
    try:
        TotalFrame_bytes = DME.read_bytes(RSBE01 + TIME_START_ADRR, 4)
        res = struct.unpack(">I", TotalFrame_bytes)[0] if TotalFrame_bytes else 0
        return res
    except Exception:
        return None


def find_matrix_position(PlayerID):
    try:
        lvl0 = DME.read_bytes(RSBE01 + 0x624780, 4)  # First p*
        Lvl1 = b"\x00\x00\x00\x00"

        match PlayerID:  # Could be optimize, if a player is not playing then you don't have to read the value
            case 0:  # p1
                address_int = int.from_bytes(lvl0[1:], byteorder="big") + RSBE01 + 0x34
                Lvl1 = DME.read_bytes(address_int, 4)
            case 1:  # p2
                address_int = int.from_bytes(lvl0[1:], byteorder="big") + RSBE01 + 0x278
                Lvl1 = DME.read_bytes(address_int, 4)
            case 2:  # p3
                address_int = int.from_bytes(lvl0[1:], byteorder="big") + RSBE01 + 0x4BC
                Lvl1 = DME.read_bytes(address_int, 4)
            case 3:  # p4
                address_int = int.from_bytes(lvl0[1:], byteorder="big") + RSBE01 + 0x700
                Lvl1 = DME.read_bytes(address_int, 4)

        # -1 mean no player, before player spawner a random default value will be set
        # Maybe trying to coordinate Start timer and player loc

        address_int = int.from_bytes(Lvl1[1:], byteorder="big") + RSBE01 + 0x1000060  # 810000 data region
        Lvl2 = DME.read_bytes(address_int, 4)

        address_int = int.from_bytes(Lvl2[1:], byteorder="big") + RSBE01 + 0x10000D8
        Lvl3 = DME.read_bytes(address_int, 4)

        address_int = int.from_bytes(Lvl3[1:], byteorder="big") + RSBE01 + 0x100000C
        Lvl4 = DME.read_bytes(address_int, 4)

        address_int = int.from_bytes(Lvl4[1:], byteorder="big") + RSBE01 + 0x100000C

        posXlvl5 = DME.read_bytes(address_int, 4)
        posYLvl5 = DME.read_bytes(address_int + 0x4, 4)

        X = struct.unpack(">f", posXlvl5)[0]
        Y = struct.unpack(">f", posYLvl5)[0]

        return [X, 0, Y]
    except RuntimeError:
        return None


# BONUS FEATURE


def enable_HUD(enabled):
    DME.write_bytes(RSBE01 + DISPLAY_HUD, struct.pack(">b", enabled))


def enable_debug_mod(enabled):
    DME.write_bytes(RSBE01 + DEBUG_MOD, struct.pack(">b", enabled))


def draw_di(enabled):
    DME.write_bytes(RSBE01 + DRAW_DI, struct.pack(">b", enabled))


def lock_camera(enabled):
    DME.write_bytes(RSBE01 + CAM_LOCK, struct.pack(">b", enabled))


def update_music_volume(value):
    # Match restart = reset volume
    DME.write_bytes(RSBE01 + MUSIC_ADDR, struct.pack(">f", (value / 100)))


def update_sound_effect_volume(value):
    # Match restart = reset volume
    DME.write_bytes(RSBE01 + SOUND_EFFECT_ADDR, struct.pack(">f", (value / 100)))


def update_FrontDepth_cam(value):
    DME.write_bytes(RSBE01 + CAM_FRONT_DEPTH_ADRR, struct.pack(">f", value))


def update_FOV_cam(value):
    # focal_length = BLCam.data.lens / 100
    DME.write_bytes(RSBE01 + CAM_FOV_ADRR, struct.pack(">f", value))


def update_BackDepth_cam(value):
    if value >= 4999:
        DME.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADDR, struct.pack(">f", 1000000))  # Max value
    else:
        DME.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADDR, struct.pack(">f", value))


def update_camera_mod(value):
    my_tool = bpy.context.scene.my_tool
    my_tool.lock_camera = True if value == 0 else False


def change_stage_debug_mod(value):
    if value == 0:  # default
        DME.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">I", 0xAC001500))
        DME.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">b", 0))
    elif value == 1:  # collision on
        DME.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">I", 0xAC001500))
        DME.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 1))
    elif value == 2:  # collision on stage off
        DME.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 2))
    elif value == 3:  # stage off
        DME.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 0))
        DME.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">B", 44))


def change_character_debug_mod(value):
    if value == 0:  # default
        DME.write_bytes(RSBE01 + DISPLAY_CHARMODEL, struct.pack(">B", 2))
        DME.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 0))
    elif value == 1:  # Hurtbox on
        DME.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 1))
    elif value == 2:  # Hurtbox on 3D model off
        DME.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 2))
    elif value == 3:  # 3D model off
        DME.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 0))
        DME.write_bytes(RSBE01 + DISPLAY_CHARMODEL, struct.pack(">B", 0))


def update_shadow_X_orientation(enabled):
    DME.write_bytes(RSBE01 + SHADOW_X_ORIENTATION_ADRR, struct.pack(">f", enabled))


def update_shadow_Y_orientation(enabled):
    DME.write_bytes(RSBE01 + SHADOW_Y_ORIENTATION_ADRR, struct.pack(">f", enabled))


def enable_character_shadow(enabled):
    # Break X or Y orientation make shadows disappear, so this is still WIP
    data = struct.pack(">f", 20) if enabled else b"\xff\xff"
    DME.write_bytes(RSBE01 + SHADOW_X_ORIENTATION_ADRR, data)
    DME.write_bytes(RSBE01 + SHADOW_Y_ORIENTATION_ADRR, data)
    # Match restart => reset shadows Orientation
    # Note : On certain stage like BF, light is auto update so it will not work.
    # BUT it should be possible to overwrite it on every frame !


def update_custom_color_background(enabled, color):
    if enabled:
        r, g, b, a = [int(c * 255) for c in color]
        color_bytes = struct.pack("BBBB", r, g, b, a)
        DME.write_bytes(RSBE01 + STAGE_GREEN_SCREEN, color_bytes)


def enable_custom_color_background(bool):
    value = [0, 1, 0] if bool else [0, 255, 1]
    addresses = [0x663104, 0x6630FD, 0x663061]  # simulate the smashball effect, thanks to EON for this !
    for val, addr in zip(value, addresses):
        DME.write_bytes(RSBE01 + addr, struct.pack(">B", val))


def update_custom_color_background_stage(enabled, color):
    if enabled:
        r, g, b, a = [int(c * 255) for c in color]
        color_bytes = struct.pack("BBBB", r, g, b, a)
        DME.write_bytes(RSBE01 + BACKGROUND_GREEN_SCREEN, color_bytes)


def play_animation(my_tool):
    print("play animation")
    bpy.ops.screen.animation_play() if my_tool else bpy.ops.screen.animation_cancel()
    # bpy.ops.screen.animation_cancel()
    # bpy.context.screen.is_animation_playing
