# Base Address de la RAM Wii (MEM1)
RSBE01 = 0x80000000

# Page size to look for in pattern_scan_all, as there could be multiple pages with RSBE01.
TIME_START_ADRR = 0x62B420  # start as soon as timer hit 0, 0 means no match
TIMER_START_MATCH_ADRR = 0x5BBFF0  # start as soon as you enter a match, start as -213, 2 byte
# CAMERA DATA
CAM_TYPE = 0x6155C9  # (bool) 0 for IG & 1 for pause (only read)
CAM_FOV_ADRR = 0x5B6DF0
CAM_FRONT_DEPTH_ADRR = 0x5B6DFC  # float value
CAM_BACK_DEPTH_ADDR = 0x5B6E00  # float value
CAM_ROT_Z_ADRR = 0x5B6DE8  # Do a Barell Roll ! value view as a radiant angle, 90° = 1,5707..
# Camera ON PAUSE
CAM_PAUSE_ORG_ADRR = 0x6636C8  # Origin pos X,Y,Z
CAM_PAUSE_ANGLE_ADRR = 0x6636D4  # X,Y radiant angle
DISTANCE_CAM_ORG_ADRR = 0x6636DC  # distance between Origin and Cam
# Camera IN GAME
CAM_IG_ORG_ADRR = 0x5B6D80  # 3 float X,Y,Z
CAM_IG_POSITION_ADRR = 0x5B6D8C  # 3 float X,Y,Z
# Absolute ORG ??
ABS_ORG_ADRR = 0x5B6DCC  # Something to do with this, maybe this is the real thing
# Camera focus point
FOCUS_CAM_PAUSE_ADRR = 0x6636E0  # FF FF FF FF = Stage, 00 00 00 00 = P1, 01 = P2, 02 = P3... Float
# When pausing + focus on stage, Origin cannot be change from Z = 0, however when your focus a player it can be modify
# But as soon as you unpause even with camLock On, the Z axis will be move again to 0
FOCUS_CAM_REPLAY_ADRR = 0x663F97  # 00 default stage, 01 = P1, 02 = P2 etc.. byte
# During replay match even with camera lock ON, if your focus is a player it will still mooves, but if the focus is the stage then camera will be lock

# MISC
STAGE_ID = 0x62B3B4
STAGE_GREEN_SCREEN = 0x663114  # RGBA
BACKGROUND_GREEN_SCREEN = 0x5B505C  # RGBA

# TOGGLE DEBUG MOD
DEBUG_MOD = 0x4E0D33  # boolean
CAM_LOCK = 0x4E0E37  # Need to lock the camera for better result
DISPLAY_HUD = 0x4E0EC3  # boolean, Stock, % and timer
DISPLAY_HURTBOX = 0x4E0D6B  # 0 = default, 1 = hurtbox On, 2 = hurtbox On + model Off
DISPLAY_CHARMODEL = 0x6732F3  # 2 default, 0 for disable   WIP could be better
DISPLAY_STAGE_COLLISION = 0x4E0DEF  # Using debug menu, 0 = default, 1 = collision On, 2 = Stage Hide + collision On
DISPLAY_STAGE = 0x6732D9  # Only disable stage, 172 = on, 44 = Off
DRAW_DI = 0x4E0E67  # boolean
MUSIC_ADDR = 0x10345718  # float   0x90345718 <-- exact memory adress in dolphin memory engine
SOUND_EFFECT_ADDR = 0x10E60F38  # float 0x90E60F38
# Shadow orientation
SHADOW_X_ORIENTATION_ADRR = 0x06733BC  # float value,
SHADOW_Y_ORIENTATION_ADRR = 0x06733C0  # float value
# Classic XYZ matrix, Z won't do anything tho,
# by breaking the value we can disable shadows, But maybe there are better ways to do it

# if ID = -1 or > 50, Hide PLAYER_X /// Position � update avec le sheet
list_playerID = [0x62131F, 0x6214E3, 0x6216A7, 0x62186B]  # 1bytes, -1 mean no char
