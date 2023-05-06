import bpy
import math
import struct
import time
from numpy import vectorize

from .emc_common import pm, RSBE01, CAM_TYPE,ID_P1, ID_P2, ID_P4, ID_P4, \
    CAM_PAUSE_PIVOT_ADRR, CAM_IG_PIVOT_ADRR,CAM_ROTATION_ADRR, DISTANCE_CAM_POINT_ADRR,CAM_ROT_Z_ADRR,\
    CAM_BACK_DEPTH_ADRR, CAM_FRONT_DEPTH_ADRR

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
    P1 = bpy.data.objects['PLAYER_1']
    P2 = bpy.data.objects['PLAYER_2']
    P3 = bpy.data.objects['PLAYER_3']
    P4 = bpy.data.objects['PLAYER_4']
            
    p1_loc = P1.matrix_world.translation
    p2_loc = P2.matrix_world.translation
    p3_loc = P3.matrix_world.translation
    p4_loc = P4.matrix_world.translation

    P1_charID = pm.read_uchar(RSBE01 + ID_P1)
    P2_charID = pm.read_uchar(RSBE01 + ID_P2)
    P3_charID = pm.read_uchar(RSBE01 + ID_P4)    
    P4_charID = pm.read_uchar(RSBE01 + ID_P4)

    if (P1_charID < 50): #check if characterID exist
       P1_BYTES = pm.read_bytes(RSBE01 + find_matrix_position(P1_charID), 12) #read position matrix
       p1_loc.xzy = struct.unpack(">fff", P1_BYTES)
       p1_loc.x = p1_loc.x * -1
       P1.hide_set(False)
    else: #if not hide asset in blender
        P1.hide_set(True)

    if (P2_charID < 50):#check if characterID exist
       P2_BYTES = pm.read_bytes(RSBE01 + 0x52000 + find_matrix_position(P2_charID), 12) #read position matrix
       p2_loc.xzy = struct.unpack(">fff", P2_BYTES)
       p2_loc.x = p2_loc.x * -1
       P2.hide_set(False)
    else: #if not hide asset in blender
        P2.hide_set(True)   

    if (P3_charID < 50):#check if characterID exist
       P3_BYTES = pm.read_bytes(RSBE01 + 0xA4000 + find_matrix_position(P3_charID), 12) #read position matrix
       p3_loc.xzy = struct.unpack(">fff", P3_BYTES)
       p3_loc.x = p3_loc.x * -1  
       P3.hide_set(False)
    else: #if not hide asset in blender
        P3.hide_set(True)       

    if (P4_charID < 50):#check if characterID exist
       P4_BYTES = pm.read_bytes(RSBE01 + 0xF6000 +find_matrix_position(P4_charID), 12) #read position matrix
       p4_loc.xzy = struct.unpack(">fff", P4_BYTES)  
       p4_loc.x = p4_loc.x * -1 
       P4.hide_set(False)
    else: #if not hide asset in blender
        P4.hide_set(True)       

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

def find_pivot_cam():
    BLorg = bpy.data.objects['Origin']
    BLorg.matrix_world.translation.y = 0
    BLorg.location[1] = 0
    BLorg.location[2] = 0
    BLorg_loc = [BLorg.matrix_world.translation.x * -1, BLorg.matrix_world.translation.z, BLorg.matrix_world.translation.y]
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            rv3d = area.spaces[0].region_3d
            if rv3d.view_perspective == 'CAMERA':
                rv3d.view_location.y = BLorg.matrix_world.translation.y

    return BLorg_loc 

def change_FrontDepth_cam(value):
    sliderValue = round(value, 1)
    print("call front: " + str(sliderValue))
    #Change the depth of camera
    pm.write_bytes(RSBE01 + CAM_FRONT_DEPTH_ADRR, struct.pack(">f",sliderValue), 4)

def change_BackDepth_cam(value):
    sliderValue = round(value, 1)
    print("call back: " + str(sliderValue))
    #Change the depth of camera
    pm.write_bytes(RSBE01 + CAM_BACK_DEPTH_ADRR, struct.pack(">f",sliderValue), 4)

def sync_blender_cam():
    # Define Camera and Pivot point
    BLorg_loc = find_pivot_cam()
    cam_loc = bpy.data.objects['Camera'].matrix_world.translation.xzy
    cam_loc.x = -cam_loc.x 
        
    if (check_cam_type() == 0x6636C8): #camera on PAUSE
        BLcam_rotation = bpy.data.objects['Camera'].rotation_euler #Radiant
        #Pivot point
        mat_bytes = b''
        for c in BLorg_loc:
            mat_bytes += struct.pack(">f", c)
        pm.write_bytes(RSBE01 + CAM_PAUSE_PIVOT_ADRR, mat_bytes, len(mat_bytes))   
        
        #Rotate arround the PivotPoint
        pm.write_bytes(RSBE01 + CAM_ROTATION_ADRR, struct.pack(">f", (BLcam_rotation.x - 1.5708) * -1), 4) #X radiant
        pm.write_bytes(RSBE01 + CAM_ROTATION_ADRR + 0x4, struct.pack(">f", BLcam_rotation.z - 3.1416), 4) #Y radiant
        pm.write_bytes(RSBE01 + CAM_ROT_Z_ADRR, struct.pack(">f", BLcam_rotation.y - 3.1416), 4) #Barell roll rotation °

        r = math.sqrt((cam_loc.x - BLorg_loc[0]) ** 2 + (cam_loc.y - BLorg_loc[1]) ** 2 + (cam_loc.z - BLorg_loc[2]) ** 2) #calcule le rayon
        pm.write_bytes(RSBE01 + DISTANCE_CAM_POINT_ADRR, struct.pack(">f",r), 4) #Distance camera
    else:       
        Cam_IG_Data = [BLorg_loc, cam_loc] 

        mat_bytes = b''
        for r in Cam_IG_Data:
            for c in r:
                mat_bytes += struct.pack(">f", c)
        pm.write_bytes(RSBE01 + CAM_IG_PIVOT_ADRR, mat_bytes, len(mat_bytes))

def get_current_frame():
    frame_bytes = pm.read_bytes(CURRENT_FRAME,2)
    CURRENT_FRAME = struct.unpack('>h', frame_bytes)
    CURRENT_FRAME = (CURRENT_FRAME[0] - 212) #work on every stage, CURRENT_FRAME = timer IG

    TotalFrame_bytes = pm.read_bytes(RSBE01 + CURRENT_FRAME,4)
    TotalFrame = struct.unpack('>f', TotalFrame_bytes)
    return CURRENT_FRAME

def find_matrix_position(CharID):
    #between each player of same CharID there is 0x52000, so if MARIO P1 position X is at 0x8125EAF0 ,
    #MARIO P2 position X will be at 0x8125EAF0 + 0x52000 = 812b0af0, P3 = P1 + a4000...
    
        match CharID: #Don't know how Pointer work so doing shitty code, sorry for that :pepepray:
            case 0: #Mario            
                return 0x125EAF0
            case 1:#Dk
                return 0x127A1E0
            case 2:#Link
                return 0x1272390
            case 3:#Samus
                return 0x1266B0C
            case 4:#...
                return 0x126A4C0
            case 5:
                return 0x127045C
            case 6:
                return 0x126F9B8
            case 7:
                return 0x1261890
            case 8:
                return 0x127E56C
            case 9:
                return 0x1280110
            case 10:
                return 0x12418B0
            case 11:
                return 0x1278570
            case 12:
                return 0x1274000
            case 13:
                return 0x1274070
            case 14:
                return 0x126C770
            case 15:
                return 0x126C614
            case 17:
                return 0x1284170
            case 18:
                return 0x126C8F0
            case 19:
                return 0x126F658
            case 20:
                return 0x127FD30
            case 21:
                return 0x127F3B4
            case 22:
                return 0x12820D0
            case 23:
                return 0x1262F30
            case 24:
                return 0x1272A30
            case 25:
                return 0x126C5B0
            case 26:
                return 0x1243A10
            case 27:
                return 0x1265E50
            case 29:
                return 0x1274758
            case 30:
                return 0x12763B0
            case 31:
                return 0x127B570
            case 32:
                return 0x1244F70
            case 33:
                return 0x127C190
            case 34:
                return 0x1281B70
            case 35:
                return 0x127A478
            case 37:
                return 0x1282C90
            case 38:
                return 0x127C190
            case 39:
                return 0x1284170
            case 41:
                return 0x1272350
            case 44:
                return 0x126FA78
            case 45:
                return 0x127F110
            case 46:
                return 0x125F6B0
            case 47:
                return 0x127F110