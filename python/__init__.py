import bpy
from .ebc_panel import classes, PointerProperty, control_properties, display_box_panel
from bpy.utils import register_class, unregister_class
from pathlib import Path

bl_info = {
    "name": "External-Brawl-Camera",
    "author": "NEYROE",
    "version": (2, 0, 0),
    "blender": (4, 3),
    "warning": "Requires installation of dependencies",
    "category": "Tools",
    "description": "Control Brawl Camera from Blender.",
    "wiki_url": "https://github.com/Neyroe/External-Brawl-Camera",
}


def register():
    for cls in classes:
        register_class(cls)
    bpy.types.Scene.my_tool = PointerProperty(type=control_properties)
    bpy.types.Scene.my_tool_display = PointerProperty(type=display_box_panel)


def unregister():
    for cls in reversed(classes):
        unregister_class(cls)
    del bpy.types.Scene.my_tool
    del bpy.types.Scene.my_tool_display


if __name__ == "__main__":
    register()
