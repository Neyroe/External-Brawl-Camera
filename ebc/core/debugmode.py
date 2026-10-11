"""Project+ Debug Mode: the game pause and frame advance of P+'s debug controls, by data. No ``bpy``.

``Game.debug`` (docs/research/debug-mode.md). Three bytes, no code patch:

* Code Menu "Debug Mode" (``DEBUG_MENU``, low byte of u32 0x804E0D30): the Code Menu copies it every
  frame into the game's flag (``DEBUG_MODE_GAME``), which enables P+'s pad controls (Start = debug
  pause, Z = frame advance, L + D-pad: hitboxes...). Written through the Code Menu overrides of
  ``Game.display`` (restored, re-applied on a new match).
* Debug pause: bit 0x02 of ``DEBUG_PAUSE`` (gfTaskScheduler +0x0B), what Start toggles in Debug Mode.
  The game logic stops (match frame, timer, fighters), Dolphin keeps running: EBC's memory writes and
  the frozen camera still reach the picture. A ``BitOverride`` of the Game: ``restore_all`` resumes
  a pause EBC set.
* Frame advance: ``DEBUG_STEP`` (gfTaskScheduler +0x0A) = 1 while paused. The next frame runs whole,
  then the game writes 0 back: exactly one game frame (MATCH_FRAME +1) per request.

The pause and the step work with Debug Mode off too (the scheduler hook is unconditional); the UI
turns Debug Mode on with them so the in-game buttons agree with EBC's.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .game import Game

STEP_TIMEOUT = 0.5  # seconds for the game to take a frame advance (a few frames at 60 Hz)
STEP_POLL = 0.002


class DebugMode:
    """``Game.debug``: Debug Mode on / off, game pause, resume, one-frame step.

    The pause bit's override is ``Game.bit_debug_pause`` (in the Game's registry, so
    ``restore_all`` resumes a pause EBC left).
    """

    def __init__(self, game: Game) -> None:
        self.game = game
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], float] = time.monotonic
        self.last_error: str | None = None
        # Last known debug pause for the UI (no I/O in draw()): True paused, False running, None unknown.
        self.known_paused: bool | None = None

    # -- Debug Mode (Code Menu) ----------------------------------------------------

    def set_enabled(self, enabled: bool) -> bool:
        """Code Menu Debug Mode. Turning it off also resumes a debug pause: without Debug Mode the
        in-game Start no longer toggles it, the game would stay stopped."""
        ok = self.game.display.set_flag(self.game.p.DEBUG_MENU, enabled)
        if not enabled:
            ok = self.resume() and ok
        return ok

    def enabled(self) -> bool | None:
        """The game's own Debug Mode flag (the Code Menu copies its value there every frame)."""
        data = self.game.read_bytes(self.game.p.DEBUG_MODE_GAME.addr, 1, fresh=True)
        return None if data is None else bool(data[0] & 0x01)

    # -- game pause ----------------------------------------------------------------

    def paused(self) -> bool | None:
        """The debug pause bit now (None when unreadable)."""
        data = self.game.read_bytes(self.game.p.DEBUG_PAUSE.addr, 1, fresh=True)
        if data is None:
            return None
        self.known_paused = bool(data[0] & self.game.p.DEBUG_PAUSE_BIT)
        return self.known_paused

    def pause(self) -> bool:
        """Stop the game logic (Dolphin keeps running). Nothing when already paused."""
        if not self.game.bit_debug_pause.apply(self.game, True):
            return self._fail("could not write the debug pause flag")
        self.known_paused = True
        self.last_error = None
        return True

    def resume(self) -> bool:
        """Let the game run again. Nothing when it runs."""
        bit = self.game.bit_debug_pause
        if not bit.apply(self.game, False):
            return self._fail("could not write the debug pause flag")
        bit.forget()  # running is the game's normal state: nothing left to restore
        self.known_paused = False
        self.last_error = None
        return True

    def toggle(self) -> bool:
        paused = self.paused()
        if paused is None:
            return self._fail("debug pause flag unreadable")
        return self.resume() if paused else self.pause()

    def step(self, wait: bool = True, timeout: float = STEP_TIMEOUT) -> bool:
        """Advance exactly one game frame. When the game runs, pause it instead (like Dolphin's Frame
        Advance). ``wait``: return once the game took the frame (it clears the request at the end of
        it); False with ``last_error`` when it did not (Dolphin itself paused, or not responding)."""
        paused = self.paused()
        if paused is None:
            return self._fail("debug pause flag unreadable")
        if not paused:
            return self.pause()
        step = self.game.p.DEBUG_STEP.addr
        if not self.game.write_bytes(step, b"\x01"):
            return self._fail("could not write the frame advance request")
        if not wait:
            self.last_error = None
            return True
        deadline = self.clock() + timeout
        while True:
            data = self.game.read_bytes(step, 1, fresh=True)
            if data is None:
                return self._fail("frame advance request unreadable")
            if data[0] == 0:
                self.last_error = None
                return True
            if self.clock() >= deadline:
                return self._fail("the game did not take the frame (is Dolphin itself paused?)")
            self.sleep(STEP_POLL)

    def note_running(self) -> None:
        """The match frame moved (sync tick): the game is not debug-paused."""
        self.known_paused = False

    def _fail(self, msg: str) -> bool:
        self.last_error = msg if self.game.connected else "not connected"
        return False
