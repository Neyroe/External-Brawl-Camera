import bpy
import math
import struct
from mathutils import Euler
from .ebc_common import *

def init_globals(context):
    # Define Camera and Pivot point
    global BLorg, BLCam
    print ("Find Camera and Origin")
    BLorg = bpy.data.objects['Origin']
    BLCam = bpy.data.objects['Camera'] 

def lock_camera_to_view(boolean):
    #Lock the camera in blender windows to only lock forward the Origin point (org)
    if boolean:
        BLorg.matrix_world.translation.y = 0 #Origin can not moove arround this axe (axis Z for smash brawl)
        BLorg.location[1] = 0 #make sure that Origin stay straight where camera is looking
        BLorg.location[2] = 0
        for area in bpy.context.screen.areas: #Make the org pos always at 0 and lock the camera arround a correct axis
            if area.type == 'VIEW_3D':
                rv3d = area.spaces[0].region_3d
                if rv3d.view_perspective == 'CAMERA':
                    rv3d.view_location.y = BLorg.matrix_world.translation.y

def sync_blender_z_cam_rotation(boolean, cam_rot):
    #Camera will always look forward if you want to do so.
    if (boolean == False):
        EMU.write_bytes(RSBE01 + CAM_ROT_Z_ADRR, struct.pack(">f", cam_rot), 4) #World or local might change how camera interact
    else: EMU.write_bytes(RSBE01 + CAM_ROT_Z_ADRR, struct.pack(">f", 0), 4) #World or local ?

def sync_blender_cam(lock_cam_toggle, cam_z_rotation_toggle):
    lock_camera_to_view(lock_cam_toggle)
    sync_blender_z_cam_rotation(cam_z_rotation_toggle, BLCam.rotation_euler.y)

    BLorg_loc = BLorg.matrix_world.translation.xzy
    BLorg_loc.x *= -1
    
    BLCam_loc = BLCam.matrix_world.translation.xzy #Should be able to do it 1 time per session
    BLCam_loc.x *= -1 

    EMU.write_bytes(RSBE01 + CAM_LOCK, b'\1', 1)

    if (check_cam_type() == 0x6636C8): #camera on PAUSE
        mat_bytes = b''
        for c in BLorg_loc:
            mat_bytes += struct.pack(">f", c)
        EMU.write_bytes(RSBE01 + CAM_PAUSE_ORG_ADRR, mat_bytes, len(mat_bytes))   
        
        #Angle from Origin to camera, in a good world camera is always looking to org
        EMU.write_bytes(RSBE01 + CAM_PAUSE_ANGLE_ADRR, struct.pack(">f", (-BLCam.matrix_world.to_euler().x - math.pi /2)), 4) #X radiant
        EMU.write_bytes(RSBE01 + CAM_PAUSE_ANGLE_ADRR + 0x4, struct.pack(">f", BLCam.matrix_world.to_euler().z ), 4) #Y radiant

        Cam_Position_To_Org = math.sqrt((BLCam_loc.x - BLorg_loc.x) ** 2 + (BLCam_loc.y - BLorg_loc.y) ** 2 + (BLCam_loc.z - BLorg_loc.z) ** 2)
        EMU.write_bytes(RSBE01 + DISTANCE_CAM_ORG_ADRR, struct.pack(">f",Cam_Position_To_Org), 4) #Distance camera to origin
    else:
        Cam_IG_Data = [BLorg_loc, BLCam_loc] 
        mat_bytes = b''
        for r in Cam_IG_Data:
            for c in r:
                mat_bytes += struct.pack(">f", c)
        EMU.write_bytes(RSBE01 + CAM_IG_ORG_ADRR, mat_bytes, len(mat_bytes))

def set_player_pos():
    liste_players = [0,0,0,0]
    liste_players[0] = bpy.data.objects['PLAYER_1']
    liste_players[1] = bpy.data.objects['PLAYER_2']
    liste_players[2] = bpy.data.objects['PLAYER_3']
    liste_players[3] = bpy.data.objects['PLAYER_4']

    ''' #Don't find player in a good order :/
    Player_Collection = bpy.data.collections.get("PLAYERS")
    for objet in Player_Collection.objects:#Add every player in list
        liste_players.append(objet)'''

    for r in range(len(liste_players)):
        if (EMU.read_uchar(RSBE01 + list_playerID[r]) < 50 and EMU.read_uchar(RSBE01 + list_playerID[r]) > 0): #be sure if they are a player to display
            liste_players[r].location = find_matrix_position(r)
            liste_players[r].location.x *= -1
            liste_players[r].hide_set(False) #Be sure to not hide on scene
        else:
            liste_players[r].hide_set(True) #Hide on scene nothing more to do

def sync_player_control(addr):
    if bpy.context.screen.is_animation_playing:
        anim_byte = 0
    else:
        anim_byte = 1
    buf = struct.pack(">b", anim_byte)
    EMU.write_bytes(addr, buf, len(buf))

def check_cam_type():
    current_type = EMU.read_uchar(RSBE01 + CAM_TYPE) #return int
    if (current_type == 0):
        return 0x5B6D80 #Camera on Match
    else:
        return 0x6636C8 #Camera on Pause

def sync_brawlCam_to_Blender():
    if (EMU.read_bytes(RSBE01 + CAM_LOCK, 1) == b'\1'): #QOL can be #
        EMU.write_bytes(RSBE01 + CAM_LOCK, b'\0', 1)
        
    # Define Camera and Pivot point
    BLorg = bpy.data.objects['Origin'] 
    BLcam = bpy.data.objects['Camera']

    brawlOrg_loc = [0,0,0]
    brawlCam_loc = [0,0,0]
    
    for i in range(3):
        byteBrawlOrg_loc = EMU.read_bytes(RSBE01 + CAM_IG_ORG_ADRR + (i * 0x4), 4)
        brawlOrg_loc[i] = struct.unpack(">f", byteBrawlOrg_loc)[0]

        byteBrawlCam_loc = EMU.read_bytes((RSBE01 + CAM_IG_POSITION_ADRR + (i * 0x4)), 4)
        brawlCam_loc[i] = struct.unpack(">f", byteBrawlCam_loc)[0]

    brawlCam_loc[0] *=-1
    brawlOrg_loc[0] *=-1
    BLcam.matrix_world.translation.xzy = brawlCam_loc
    BLorg.matrix_world.translation.xzy = brawlOrg_loc

    '''if (BLcam.constraints.find('Track Origin') == -1): #QOL nothing more
        track_constraint = BLcam.constraints.new('TRACK_TO')
        track_constraint.name = "Track Origin"
        track_constraint.influence = 0
        track_constraint.target = BLorg
        track_constraint.track_axis = 'TRACK_NEGATIVE_Z' 
        track_constraint.up_axis = 'UP_Y' ''' 

def get_current_frame():
    TotalFrame_bytes = EMU.read_bytes(RSBE01 + TIME_START_ADRR, 4)
    res = struct.unpack('>I', TotalFrame_bytes)[0] if TotalFrame_bytes else 0
    return res

def find_matrix_position(PlayerID):    
    lvl0 = EMU.read_bytes(RSBE01 + 0x624780, 4) #First p*
    Lvl1 = 0x0

    match PlayerID: #Could be optimize, if a player is not playing then you don't have to read the value
        case 0: #p1 
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x34
            Lvl1 = EMU.read_bytes(address_int, 4)
        case 1: #p2
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x278
            Lvl1 = EMU.read_bytes(address_int, 4)
        case 2: #p3
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x4BC
            Lvl1 = EMU.read_bytes(address_int, 4)
        case 3: #p4
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x700
            Lvl1 = EMU.read_bytes(address_int, 4)

    #-1 mean no player, before player spawner a random default value will be set
    #Maybe trying to coordinate Start timer and player loc 

    address_int = int.from_bytes(Lvl1[1:], byteorder='big') + RSBE01 + 0x1000060 #810000 data region
    Lvl2 = EMU.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl2[1:], byteorder='big') + RSBE01 + 0x10000D8
    Lvl3 = EMU.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl3[1:], byteorder='big') + RSBE01 + 0x100000C
    Lvl4 = EMU.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl4[1:], byteorder='big') + RSBE01 + 0x100000C

    posXlvl5 = EMU.read_bytes(address_int, 4)
    posYLvl5 = EMU.read_bytes(address_int + 0x4, 4)

    X = struct.unpack('>f', posXlvl5)[0]
    Y = struct.unpack('>f', posYLvl5)[0]

    posPlayer = [X, 0, Y]
    return posPlayer

#BONUS FEATURE

def enable_HUD(value):    
    EMU.write_bytes(RSBE01 + DISPLAY_HUD, struct.pack(">b",value), 1)
    
def enable_debug_mod(value):    
    EMU.write_bytes(RSBE01 + DEBUG_MOD, struct.pack(">b",value), 1)

def draw_di(value):    
    EMU.write_bytes(RSBE01 + DRAW_DI, struct.pack(">b",value), 1)

def update_music_volume(value):    
    # Match restart = reset volume
    EMU.write_bytes(RSBE01 + MUSIC_ADDR, struct.pack(">f",(value/100)), 4)

def update_sound_effect_volume(value):
    # Match restart = reset volume
    EMU.write_bytes(RSBE01 + SOUND_EFFECT_ADDR, struct.pack(">f",(value/100)), 4)

def update_FrontDepth_cam(value):    
    EMU.write_bytes(RSBE01 + CAM_FRONT_DEPTH_ADRR, struct.pack(">f",value), 4)

def update_BackDepth_cam(value):  
    if (value >= 4999):
        EMU.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADDR, struct.pack(">f",1000000), 4) #Max value 
    else: EMU.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADDR, struct.pack(">f",value), 4)

def change_stage_debug_mod(value):
    if value == 0: #default
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">I", 0xAC001500), 4)
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">b", 0), 1) 
    elif value == 1: #collision on
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">I", 0xAC001500), 4)
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 1), 1)
    elif value == 2: #collision on stage off
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 2), 1)
    elif value == 3: #stage off
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE_COLLISION, struct.pack(">B", 0), 1) 
        EMU.write_bytes(RSBE01 + DISPLAY_STAGE, struct.pack(">B", 44), 4)

def change_character_debug_mod(value):
    if value == 0: #default
        EMU.write_bytes(RSBE01 + DISPLAY_CHARMODEL, struct.pack(">B", 2), 1)
        EMU.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 0), 1) 
    elif value == 1: #Hurtbox on
        EMU.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 1), 1)
    elif value == 2: #Hurtbox on 3D model off
        EMU.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 2), 1)
    elif value == 3: #3D model off
        EMU.write_bytes(RSBE01 + DISPLAY_HURTBOX, struct.pack(">B", 0), 1) 
        EMU.write_bytes(RSBE01 + DISPLAY_CHARMODEL, struct.pack(">B", 0), 1)

def update_shadow_X_orientation(value): 
    EMU.write_bytes(RSBE01 + SHADOW_X_ORIENTATION_ADRR, struct.pack(">f",value), 4)

def update_shadow_Y_orientation(value):  
    EMU.write_bytes(RSBE01 + SHADOW_Y_ORIENTATION_ADRR, struct.pack(">f",value), 4)

def enable_character_shadow(value):
    # Match restart = reset shadows Orientation
    if (value == False):
        EMU.write_bytes(RSBE01 + SHADOW_X_ORIENTATION_ADRR, b'\xFF\xFF', 2)
        EMU.write_bytes(RSBE01 + SHADOW_Y_ORIENTATION_ADRR, b'\xFF\xFF', 2)
        #Break X or Y orientation make shadows disapear, so this is still WIP
    else: 
        EMU.write_bytes(RSBE01 + SHADOW_X_ORIENTATION_ADRR, struct.pack(">f", 20), 4)
        EMU.write_bytes(RSBE01 + SHADOW_Y_ORIENTATION_ADRR, struct.pack(">f", 20), 4)
    #Note : On certain stage like BF, light is auto update so it will not work.
    #BUT it should be possible to overwrite it on every frame !

def update_custom_color_background(toggle, color):
    if toggle:
        r, g, b, a = [int(c * 255) for c in color]
        color_bytes = struct.pack('BBBB', r, g, b, a)
        EMU.write_bytes(RSBE01 + STAGE_GREEN_SCREEN, color_bytes, 4)

def enable_custom_color_background(bool):
    value = [0, 1, 0] if bool else [0, -1, 1]
    addresses = [0x663104, 0x6630FD, 0x663061] #simulate the smashball effect, thanks to EON for this !
    for val, addr in zip(value, addresses):
        EMU.write_bytes(RSBE01 + addr, struct.pack(">B", val), 1)
