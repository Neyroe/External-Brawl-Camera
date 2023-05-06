import pymem
import bpy
pm = pymem.Pymem("Dolphin.exe")
# Page size to look for in pattern_scan_all, as there could be multiple pages with RSBE01.
EMU_SIZE = 0x2000000

# Brawl specific offsets. These will always be found x distance from RSBE01 (start of emu)
TIME_START = 0x62B420 #start as timer goes work on every stage (0 mean match didn't start)
CAM_TYPE = 0x6155C9 #(bool) 0 for IG & 1 for pause (only read)
TIMER_IN_PAUSE_ADRR = 0x167EE22 #1 byte, first frame of pause = 30
TIMER_START_MATCH = 0x5BBFF0 #start as -213, 2 byte

#CAMERA DATA
CAM_FRONT_DEPTH_ADRR = 0x5B6DFC #float value
CAM_BACK_DEPTH_ADRR = 0x5B6E00 #float value
#Camera ON PAUSE
CAM_PAUSE_PIVOT_ADRR = 0x6636C8 #Pivot point adress
CAM_ROTATION_ADRR = 0x6636D4 #X & Y radiant
DISTANCE_CAM_POINT_ADRR = 0x6636DC #distance du pivot point
CAM_ROT_Z_ADRR = 0x5B6DE8 #Do a Barell Roll !

#FOCUS_CAM_ADRR = 0x6636E0
#CAM_FOCUS_STAGE_BARRAY = b'\0xFF\0xFF\0xFF\0xFF\0x00\x00\x00\x00\x41\xA0\x00\x00'
#CAM_FOCUS_STAGE_BARRAY = bytearray([0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x41, 0x20, 0x00, 0x00,])
#Camera IN GAME
CAM_IG_PIVOT_ADRR = 0x5B6D80

#MISC
STAGE_ID = 0x62B3B4 
CAM_LOCK_DEBUG = 0x4E0B66 # from 0 to 1 for locking the camera debug menu
MUSIC_ON_OFF = 0x10E60F34 # 1 - 0   0x90E60F34
SOUND_EFFECT_ON_OFF = 0x10E60F38 # 1 - 0   0x90E60F38

#if ID = -1 or > 50, Hide PLAYER_X /// Position � update avec le sheet
ID_P1 = 0x62131F #1bytes, -1 mean no char
ID_P2 = 0x6214E3
ID_P3 = 0x6216A7
ID_P4 = 0x62186B

# Finds the specific page with the size of EMU_SIZE.
def pattern_scan_all(handle, pattern, *, return_multiple=False):
    next_region = 0
    found = []

    while next_region < 0x7FFFFFFF0000:
        next_region, page_found = pymem.pattern.scan_pattern_page(
            handle,
            next_region,
            pattern,
            return_multiple=return_multiple
        )

        if not return_multiple and page_found:
            if (next_region - page_found) == int(EMU_SIZE):
                return page_found

        if page_found:
            if (next_region - page_found) == int(EMU_SIZE):
                found += page_found

    if not return_multiple:
        return None

    return found

# Finds 'RSBE01' in memory.
# This is used to jump to specific functions in Brawl ie: RSBE01 + CAM_START
def find_RSBE01():
    handle = pm.process_handle
    byte_pattern = bytes.fromhex("52 53 42 45 30 31 00 01") #RSBE01 byte array
    found = pattern_scan_all(handle, byte_pattern)
    return found

def find_dolphin_funcs(byte_pattern):
    handle = pm.process_handle
    module = pymem.process.module_from_name(pm.process_handle, "Dolphin.exe")
    found = pymem.pattern.pattern_scan_module(handle, module, byte_pattern, return_multiple=False)
    return found

RSBE01 = find_RSBE01()
CURRENT_FRAME = RSBE01 + TIME_START