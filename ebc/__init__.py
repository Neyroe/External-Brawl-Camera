"""External Brawl Camera: drive the Super Smash Bros. Brawl camera from Blender.

Blender extension entry point: registration only. The metadata lives in
``blender_manifest.toml``; the game logic in ``core/`` (no ``bpy``).

The UI modules are imported inside ``register()`` so that ``ebc.core`` stays
importable outside Blender (pytest, CI).
"""


def _classes():
    from .ui import operators, panels, props

    return (*props.classes, *operators.classes, *panels.classes)


def register() -> None:
    import bpy

    from .ui import props

    for cls in _classes():
        bpy.utils.register_class(cls)
    props.register_props()


def unregister() -> None:
    import bpy

    from . import runtime
    from .ui import props, sync

    sync.stop()
    runtime.get_game().disconnect()
    runtime.devserver.close()
    props.unregister_props()
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
