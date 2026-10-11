"""Screenshot button: Dolphin's own HD capture (Take Screenshot hotkey through the DSU pad).

The work is ``core.emucontrol.ScreenshotJob``; this module starts it, drives it
from a ``bpy.app.timers`` callback (the UI never blocks) and reports the final
path in the status bar. Paused game: the camera is rewritten (one sync tick)
before each of the four Frame Advances the capture needs.
"""

from __future__ import annotations

import os
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

import bpy

from .. import runtime
from ..core import emucontrol
from . import props, sync, util

INTERVAL = 0.01


@dataclass
class ShotState:
    job: emucontrol.ScreenshotJob | None = None  # the screenshot being taken
    scene: str | None = None
    message: str | None = None  # shown in the panel
    error: str | None = None


_state = ShotState()


def busy() -> bool:
    return _state.job is not None


def last_message() -> str | None:
    return _state.message


def destination(s, dolphin_shots: Path | None) -> Path | None:
    """The Screenshot folder: Dolphin's own when the field is empty (``dolphin_shots``, None when
    unknown), relative to the .blend for //, else the temporary folder when unsaved."""
    folder = s.screenshot_folder.strip()
    if not folder:
        return dolphin_shots
    if folder.startswith("//"):
        if not bpy.data.filepath:
            return Path(tempfile.gettempdir()) / "EBC screenshots"
        folder = bpy.path.abspath(folder)
    return Path(os.path.expanduser(folder))


def dolphin_folder() -> Path | None:
    """Dolphin's screenshot folder for Brawl (RSBE01), for the panel: no memory access."""
    paths = runtime.paths()
    return None if paths is None else paths.screenshots("RSBE01")


def start(context) -> str | None:
    """Start a screenshot. Returns an error message, or None."""
    global _state
    if busy():
        return "A screenshot is already being taken"
    game = runtime.get_game()
    if not game.connected:
        return "Not connected to Dolphin"
    if not runtime.dsu.connected:
        return "Dolphin does not listen to EBC's DSU pad: Configure Dolphin for EBC (Dolphin closed), then restart it"
    paths = runtime.paths()
    if paths is None:
        return "Dolphin folder unknown: set it in the EBC preferences"
    gid = game.game_id()
    if gid is None:
        return "Game ID unreadable"
    s = props.settings(context)
    scene = context.scene
    scene_name = scene.name
    base = emucontrol.screenshot_base(s.stage_name, scene_name)  # the stage when it is identified

    def name(frame):
        return emucontrol.screenshot_name(base, frame)

    def before_advance():
        if sync.is_running():
            sync.tick(bpy.data.scenes.get(scene_name) or bpy.context.scene)

    job = emucontrol.ScreenshotJob(
        runtime.dsu,
        paths.screenshots(gid),
        destination(s, paths.screenshots(gid)),
        name,
        runtime.running_counter,
        game.current_frame,
        before_advance,
    )
    _state = ShotState(job, scene_name, "Taking a screenshot…")
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=INTERVAL, persistent=False)
    return None


def pump() -> emucontrol.ShotResult | None:
    """One step of the running job (the timer; tools call it in background mode)."""
    job = _state.job
    if job is None:
        return None
    result = job.step()
    if result is None:
        return None
    _state.job = None
    if result.path is not None:
        how = " (paused: the game advanced 4 frames)" if result.paused else ""
        _state.message, _state.error = f"Screenshot saved: {result.path}{how}", None
        util.notify("INFO", _state.message)
    else:
        _state.message, _state.error = f"Screenshot failed: {result.error}", result.error
        util.notify("WARNING", _state.message)
    util.tag_redraw()
    return result


def stop() -> None:
    _state.job = None
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)


def _timer():
    try:
        pump()
    except Exception as exc:  # never raise from a timer  # noqa: BLE001
        traceback.print_exc()
        _state.job, _state.message, _state.error = None, f"Screenshot failed: {exc}", str(exc)
        return None
    return INTERVAL if _state.job is not None else None
