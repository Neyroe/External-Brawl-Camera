"""Process-wide runtime state shared by the UI modules.

Holds the single ``Game`` (memory backend), the ``DevServerClient``, the DSU pad
server (Dolphin's hotkeys) and the Dolphin folders found for it. Tests and the
headless smoke test swap the backend with ``set_backend(FakeBackend())``.
"""

from __future__ import annotations

from .core import emucontrol
from .core.backend import DMEBackend, MemoryBackend
from .core.devserver import DevServerClient
from .core.dolphin_config import DolphinPaths
from .core.dsu import DSUServer
from .core.game import Game

devserver = DevServerClient()


def _wire(g: Game) -> Game:
    """Skip stage intro camera writes code through the DevServer when it answers."""
    g.intro.code_writer = devserver.write_code
    g.intro.patch_available = lambda: devserver.available
    return g


game = _wire(Game(DMEBackend()))
dsu = DSUServer()
# Folders of the Dolphin EBC is connected to (found on Connect), and the preference's folder
# (manual, wins). Set by the UI; read by the DSU control and the screenshot.
dolphin_paths: DolphinPaths | None = None
manual_paths: DolphinPaths | None = None


# Last known emulation state for the Play / Pause button (no I/O in draw): True running, False
# paused, None unknown. Set by the emulator control actions, the DevServer status and the sync tick.
emulation_running: bool | None = None


def note_running(running: bool | None) -> None:
    global emulation_running
    if running is not None:
        emulation_running = running


def set_backend(backend: MemoryBackend) -> Game:
    """Replace the memory backend (keeps a fresh Game, returns it)."""
    global game
    game = _wire(Game(backend))
    return game


def get_game() -> Game:
    return game


def paths() -> DolphinPaths | None:
    return manual_paths or dolphin_paths


def running_counter() -> int | None:
    """``Game.running_counter`` of the current game (the backend may be swapped)."""
    return get_game().running_counter()


dsu_control = emucontrol.DSUControl(dsu, running_counter, lambda: get_game().game_id(), paths)


def control_kind() -> str:
    """``emucontrol.DEVSERVER`` / ``DSU`` / ``NONE`` from the last known states (no I/O)."""
    return emucontrol.choose(devserver.available, dsu.connected)


def control():
    """The emulator control backend: the DevServer client, the DSU control, or None."""
    kind = control_kind()
    return devserver if kind == emucontrol.DEVSERVER else dsu_control if kind == emucontrol.DSU else None
