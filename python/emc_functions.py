import bpy
import math
import struct
from mathutils import Euler

from .emc_common import pm, RSBE01, list_playerID, TIME_START_ADRR, \
    CAM_TYPE, CAM_PAUSE_PIVOT_ADRR, CAM_IG_PIVOT_ADRR,CAM_PAUSE_ROTATION_ADRR, DISTANCE_CAM_POINT_ADRR, CAM_ROT_Z_ADRR,\
    CAM_BACK_DEPTH_ADRR, CAM_FRONT_DEPTH_ADRR, DEBUG_CAMLOCK_ADRR

# Injects a python interpreter, so we can call functions from Dolphins main thread via offset
# Reusable shellcode using fstring formats
def call_native_func(fnc_ptr, fnc_type, fnc_args):
    pm.inject_python_interpreter()
    fnc_adr = '0x' + format((pm.base_address + fnc_ptr), "08X")
    shell_code = """import ctypes
functype = ctypes.CFUNCTYPE({})
func = functype({})
func({})""".format(fnc_type, fnc_adr, fnc_args)
    pm.inject_python_shellcode(shell_code)

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

    print ("How many player : " + str(len(liste_players)))

    for r in range(len(liste_players)):
        print ("boucle : " + str(r) + ", PLayer : " + str(liste_players[r]))
        if (pm.read_uchar(RSBE01 + list_playerID[r]) < 50 and pm.read_uchar(RSBE01 + list_playerID[r]) > 0): #be sure if they are a player to display
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
    pm.write_bytes(addr, buf, len(buf))

def check_cam_type():
    current_type = pm.read_uchar(RSBE01 + CAM_TYPE) #return int
    if (current_type == 0):
        return 0x5B6D80 #Camera on Match
    else:
        return 0x6636C8 #Camera on Pause

def change_FrontDepth_cam(value):    #Change the depth of camera
    sliderValue = round(value, 1)
    pm.write_bytes(RSBE01 + CAM_FRONT_DEPTH_ADRR, struct.pack(">f",sliderValue), 4)

def change_BackDepth_cam(value):    #Change the depth of camera
    sliderValue = round(value, 1)
    if (sliderValue >= 4999):
        pm.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADRR, struct.pack(">f",1000000), 4) #For Better precision 
        print("call : " + str(sliderValue))
    else: pm.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADRR, struct.pack(">f",sliderValue), 4)

def find_pivot_cam():
    BLorg = bpy.data.objects['Origin'] #shoud be able to just search one time and not every frame..
    BLorg.matrix_world.translation.y = 0 #Origin can not moove arround this axe (axis Z for smash brawl)
    BLorg.location[1] = 0 #make sure that Origin stay straight where camera is looking
    BLorg.location[2] = 0
    BLorg_loc = [BLorg.matrix_world.translation.x * -1, BLorg.matrix_world.translation.z, BLorg.matrix_world.translation.y]
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            rv3d = area.spaces[0].region_3d
            if rv3d.view_perspective == 'CAMERA':
                rv3d.view_location.y = BLorg.matrix_world.translation.y

    return BLorg_loc 

def sync_blender_cam():
    pm.write_bytes(RSBE01 + DEBUG_CAMLOCK_ADRR, b'\1', 1)
    # Define Camera and Pivot point
    BLorg_loc = find_pivot_cam() 
    cam_loc = bpy.data.objects['Camera'].matrix_world.translation.xzy #Should be able to do it 1 time per session
    cam_loc.x = -cam_loc.x 
        
    if (check_cam_type() == 0x6636C8): #camera on PAUSE
        BLcam_rotation = bpy.data.objects['Camera'].rotation_euler
        mat_bytes = b''
        for c in BLorg_loc:
            mat_bytes += struct.pack(">f", c)
        pm.write_bytes(RSBE01 + CAM_PAUSE_PIVOT_ADRR, mat_bytes, len(mat_bytes))   
        
        #Rotate arround the PivotPoint
        pm.write_bytes(RSBE01 + CAM_PAUSE_ROTATION_ADRR, struct.pack(">f", (BLcam_rotation.x - 1.5708) * -1), 4) #X radiant
        pm.write_bytes(RSBE01 + CAM_PAUSE_ROTATION_ADRR + 0x4, struct.pack(">f", BLcam_rotation.z - 3.1416), 4) #Y radiant
        pm.write_bytes(RSBE01 + CAM_ROT_Z_ADRR, struct.pack(">f", BLcam_rotation.y - 3.1416), 4) #Barell roll rotation °

        r = math.sqrt((cam_loc.x - BLorg_loc[0]) ** 2 + (cam_loc.y - BLorg_loc[1]) ** 2 + (cam_loc.z - BLorg_loc[2]) ** 2) #calculates the radius
        pm.write_bytes(RSBE01 + DISTANCE_CAM_POINT_ADRR, struct.pack(">f",r), 4) #Distance camera
    else:       
        Cam_IG_Data = [BLorg_loc, cam_loc] 

        mat_bytes = b''
        for r in Cam_IG_Data:
            for c in r:
                mat_bytes += struct.pack(">f", c)
        pm.write_bytes(RSBE01 + CAM_IG_PIVOT_ADRR, mat_bytes, len(mat_bytes))

def sync_brawlCam_toBlender():
    pm.write_bytes(RSBE01 + DEBUG_CAMLOCK_ADRR, b'\0', 1)
 
    # Define Camera and Pivot point
    BLorg = bpy.data.objects['Origin'] 
    cam_loc = bpy.data.objects['Camera']

    if (check_cam_type() == 0x6636C8): #camera on PAUSE        
        print("Camera in Pause State, sorry this feature is not ready yet")
    else:      #CAMERA IN GAME
        brawlOrg_loc = [0,0,0]
        brawlCam_loc = [0,0,0]

        for i in range(3):
            byteBrawlOrg_loc = pm.read_bytes(RSBE01 + CAM_IG_PIVOT_ADRR + (i * 0x4), 4)
            brawlOrg_loc[i] = struct.unpack(">f", byteBrawlOrg_loc)[0]

            byteBrawlCam_loc = pm.read_bytes((RSBE01 + 0x5B6D8C + (i * 0x4)), 4)
            brawlCam_loc[i] = struct.unpack(">f", byteBrawlCam_loc)[0]#radiant to °

        BLorg.matrix_world.translation.x = -brawlOrg_loc[0]
        BLorg.matrix_world.translation.y = brawlOrg_loc[2]
        BLorg.matrix_world.translation.z = brawlOrg_loc[1]

        cam_loc.matrix_world.translation.x = -brawlCam_loc[0]
        cam_loc.matrix_world.translation.y = brawlCam_loc[2]
        cam_loc.matrix_world.translation.z = brawlCam_loc[1]
        cam_loc.rotation_euler = Euler((math.radians(90), math.radians(0), math.radians(-180)), 'XYZ')

def get_current_frame():
    TotalFrame_bytes = pm.read_bytes(RSBE01 + TIME_START_ADRR,4)
    TotalFrame = struct.unpack('>I', TotalFrame_bytes)[0]
    return TotalFrame

def find_matrix_position(PlayerID):    
    lvl0 = pm.read_bytes(RSBE01 + 0x624780, 4) #First pointeur
    Lvl1 = 0x0

    match PlayerID: #Could optimize if a player is not playing then you don't have to calculate everything
        case 0: #p1 
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x34
            Lvl1 = pm.read_bytes(address_int, 4)
        case 1: #p2
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x278
            Lvl1 = pm.read_bytes(address_int, 4)
        case 2: #p3
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x4BC
            Lvl1 = pm.read_bytes(address_int, 4)
        case 3: #p4
            address_int = int.from_bytes(lvl0[1:], byteorder='big')+ RSBE01 + 0x700
            Lvl1 = pm.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl1[1:], byteorder='big') + RSBE01 + 0x1000060 #810000 data region
    Lvl2 = pm.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl2[1:], byteorder='big') + RSBE01 + 0x10000D8
    Lvl3 = pm.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl3[1:], byteorder='big') + RSBE01 + 0x100000C
    Lvl4 = pm.read_bytes(address_int, 4)

    address_int = int.from_bytes(Lvl4[1:], byteorder='big') + RSBE01 + 0x100000C
    lvl5 = pm.read_bytes(address_int, 4)
    yLvl5 = pm.read_bytes(address_int + 0x4, 4)

    X = struct.unpack('>f', lvl5)[0]
    Y = struct.unpack('>f', yLvl5)[0]
    posPlayer = [X, 0, Y]
    return posPlayer