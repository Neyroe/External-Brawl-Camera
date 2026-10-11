"""Small Blender helpers shared by the UI modules (no state, no import of the other UI modules)."""

import bpy


def tag_redraw() -> None:
    """Redraw the 3D views (where the EBC panels are)."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def notify(level: str, message: str) -> None:
    """A message from a background task: the console, and the status bar when there is a window."""
    print(f"[EBC] {message}", flush=True)
    try:
        bpy.ops.ebc.notify(level=level, message=message)
    except Exception:  # no window (background) or unregistered: the console line is enough  # noqa: BLE001
        pass


def remove_active(owner, collection: str, index: str) -> None:
    """Remove the selected row of a UI list (``owner.<collection>[owner.<index>]``), keep the index in range."""
    rows = getattr(owner, collection)
    rows.remove(getattr(owner, index))
    setattr(owner, index, max(0, min(getattr(owner, index), len(rows) - 1)))
