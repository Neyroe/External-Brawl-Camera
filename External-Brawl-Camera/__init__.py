import bpy
from .emc_panel import classes, PointerProperty, control_properties
from bpy.utils import register_class, unregister_class

bl_info = {
    "name": "External-Brawl-Camera",
    "author": "Neyroe",
    "version": (1.0),
    "blender": (3, 3, 3),
    "location": "View3D > N",
    "description": "Control Melee's Camera from Blender.",
    "wiki_url": "https://github.com/sadkellz/External-Melee-Camera",
}

def register():
    for cls in classes:
        register_class(cls)
    bpy.types.Scene.my_tool = PointerProperty(type=control_properties)

def unregister():
    for cls in reversed(classes):
        unregister_class(cls)
    del bpy.types.Scene.my_tool

if __name__ == "__main__":
    register()