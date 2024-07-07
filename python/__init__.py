import bpy
from .ebc_panel import classes, PointerProperty, control_properties
from bpy.utils import register_class, unregister_class

bl_info = {
    "name": "External-Brawl-Camera",
    "author": "NEYROE",
    "version": (1, 2, 0),
    "blender": (3, 3),
    "warning": "Requires installation of dependencies",
    "category": "Tools",
    "description": "Control Brawl Camera from Blender.",
    "wiki_url": "https://github.com/Neyroe/External-Brawl-Camera",
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