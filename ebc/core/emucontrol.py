"""Emulator control without the DevServer: Dolphin's own hotkeys through the DSU pad. No ``bpy``.

``DSUControl`` has the DevServer client's interface (``pause``, ``resume``,
``advance_frame``, ``save_state``, ``load_state``, ``last_error``) so the UI uses
either. ``choose()`` picks the DevServer when it answers, else the DSU pad.

Measured on the P+ 3.2 AppImage (docs/research/screenshot.md 1.5):

* Frame Advance (``PS``): exactly +1 frame per press when paused; when the
  game runs, a press pauses WITHOUT advancing (``Core::DoFrameStep``). It is
  the idempotent pause.
* Toggle Pause (``Options``): there is no separate Pause / Resume hotkey, so the
  state is read in memory: a frame counter that moves in 100 ms = running.
* Save / Load State Slot 10 (``L3`` / ``R3``): EBC's own slot; the file
  ``<data>/StateSaves/<GameID>.s10`` is copied from / to EBC's state files.

``ScreenshotJob`` is the non-blocking screenshot: running -> Take Screenshot,
then the new PNG of ``ScreenShots/<GameID>/`` once its ``IEND`` chunk is written;
paused -> 2 Frame Advance, Take Screenshot, 2 Frame Advance, the camera rewritten
before each advance. Measured on the P+ 3.2 AppImage (docs/research/screenshot.md 8):
the request is served by the 2nd presented frame (dual core presents one frame
late), and a camera written while paused reaches the picture only from the 3rd
advance after it (4th when the freeze itself was set while paused): the first
advances finish the frame the pause interrupted and let the frozen camera take
over. The game ends ``SHOT_ADVANCES`` (4) frames later.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path

from . import dsu as d
from .dolphin_config import DolphinPaths


class ControlKind(StrEnum):
    DEVSERVER = "devserver"
    DSU = "dsu"
    NONE = "none"


DEVSERVER = ControlKind.DEVSERVER
DSU = ControlKind.DSU
NONE = ControlKind.NONE
LABELS = {DEVSERVER: "DevServer", DSU: "Dolphin hotkeys (DSU)", NONE: "not configured"}

RUNNING_WINDOW = 0.1  # seconds: a counter that moves within it = the game runs
SAVE_TIMEOUT = 10.0
SHOT_TIMEOUT = 5.0
SHOT_ADVANCES = 4  # frames a paused screenshot advances the game

CounterFn = Callable[[], "int | None"]


def choose(devserver_available: bool, dsu_available: bool) -> str:
    """The control backend: the DevServer when it answers, else the DSU pad, else none."""
    if devserver_available:
        return DEVSERVER
    if dsu_available:
        return DSU
    return NONE


def png_complete(path: Path) -> bool:
    """The PNG ends with its IEND chunk (Dolphin writes it from a worker thread)."""
    try:
        with open(path, "rb") as f:
            f.seek(-12, os.SEEK_END)
            return f.read(12)[4:8] == b"IEND"
    except OSError:
        return False


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def snapshot(directory: Path, pattern: str = "*.png") -> dict[Path, int]:
    try:
        return {p: p.stat().st_mtime_ns for p in Path(directory).glob(pattern)}
    except OSError:
        return {}


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "scene"


def screenshot_name(base: str, frame: int | None, when: float | None = None) -> str:
    """``<base>_<game frame>_<YYYYmmdd-HHMMSS>.png``; ``base``: the stage name, else ``EBC_<scene>``."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when if when is not None else time.time()))
    return f"{safe_name(base)}_{frame if frame is not None else 'na'}_{stamp}.png"


def screenshot_base(stage: str, scene: str) -> str:
    """The stage being played names the file (``Battlefield (L)``); unknown (menus): ``EBC_<scene>``."""
    return stage.strip() or f"EBC_{safe_name(scene)}"


def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    stem, ext = os.path.splitext(name)
    n = 1
    while path.exists():
        path = folder / f"{stem}_{n}{ext}"
        n += 1
    return path


class DSUControl:
    """Pause / resume / frame advance / savestates through Dolphin's hotkeys (DSU pad)."""

    def __init__(
        self,
        server: d.DSUServer,
        counter: CounterFn,
        game_id: Callable[[], "str | None"],
        paths: Callable[[], "DolphinPaths | None"],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self.counter = counter  # a counter that moves every frame while the emulation runs
        self.game_id = game_id
        self.paths = paths
        self.sleep = sleep
        self.clock = clock
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self.server.connected

    def _fail(self, msg: str) -> bool:
        self.last_error = msg
        return False

    def _press(self, button: str) -> bool:
        if not self.server.press(button):
            return self._fail("Dolphin does not listen to EBC's DSU pad (Configure Dolphin for EBC, restart it)")
        self.last_error = None
        return True

    def is_running(self, window: float = RUNNING_WINDOW) -> bool | None:
        """True when the counter moves within ``window`` seconds, None when it cannot be read."""
        start = self.counter()
        if start is None:
            return None
        deadline = self.clock() + window
        while self.clock() < deadline:
            self.sleep(0.01)
            now = self.counter()
            if now is None:
                return None
            if now != start:
                return True
        return False

    def pause(self, seen_running: bool = False) -> bool:
        """Frame Advance when the game runs: it pauses without advancing. Nothing when paused.

        ``seen_running``: the caller just saw the frame move (Pause at frame, in the sync tick), so
        the press is sent at once, without watching the counter for ``RUNNING_WINDOW``."""
        running = True if seen_running else self.is_running()
        if running is False:
            self.last_error = None
            return True
        return self._press(d.FRAME_ADVANCE)

    def resume(self) -> bool:
        """Toggle Pause when the game is paused. Nothing when it runs."""
        running = self.is_running()
        if running is True:
            self.last_error = None
            return True
        return self._press(d.TOGGLE_PAUSE)

    def advance_frame(self, read_frame: CounterFn | None = None, timeout: float = 0.25) -> bool:
        """One Frame Advance (+1 frame when paused, a pause when running)."""
        read = read_frame or self.counter
        start = read()
        if not self._press(d.FRAME_ADVANCE):
            return False
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            now = read()
            if now is not None and now != start:
                return True
            self.sleep(0.002)
        self.last_error = "the game frame did not change (it was running and is now paused, or not in a match)"
        return True

    def toggle_pause(self) -> bool:
        return self._press(d.TOGGLE_PAUSE)

    def _slot_file(self) -> Path | None:
        paths = self.paths()
        if paths is None:
            self._fail("Dolphin folder unknown: set it in the EBC preferences")
            return None
        gid = self.game_id()
        if not gid:
            self._fail("game ID unreadable")
            return None
        return paths.slot_file(gid)

    def save_state(self, path: str, timeout: float = SAVE_TIMEOUT) -> bool:
        """Save State Slot 10, wait until Dolphin rewrote the file (size stable), copy it to ``path``."""
        slot = self._slot_file()
        if slot is None:
            return False
        try:
            old = slot.stat().st_mtime_ns
        except OSError:
            old = None
        if not self._press(d.SAVE_SLOT):
            return False
        deadline = self.clock() + timeout
        size = -1
        while self.clock() < deadline:
            try:
                st = slot.stat()
            except OSError:
                self.sleep(0.02)
                continue
            if st.st_mtime_ns != old:
                if st.st_size == size and size > 0:
                    try:
                        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                        shutil.copy2(slot, path)
                    except OSError as exc:
                        return self._fail(f"copy of {slot} failed: {exc}")
                    self.last_error = None
                    return True
                size = st.st_size
                self.sleep(0.05)
                continue
            self.sleep(0.01)
        return self._fail(f"Dolphin did not write {slot} (Save State Slot {d.EBC_SLOT} hotkey bound?)")

    def load_state(self, path: str) -> bool:
        """Copy ``path`` over slot 10, then Load State Slot 10 (Dolphin loads it asynchronously)."""
        if not os.path.isfile(path):
            return self._fail(f"no state file {path}")
        slot = self._slot_file()
        if slot is None:
            return False
        try:
            slot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, slot)
        except OSError as exc:
            return self._fail(f"copy to {slot} failed: {exc}")
        return self._press(d.LOAD_SLOT)


class ShotPhase(Enum):
    PROBE = "probe"  # running or paused?
    ALIGN = "align"  # paused: a Frame Advance before Take Screenshot
    ALIGN_NEXT = "align next"  # ... wait before the next one
    SHOOT = "shoot"  # Take Screenshot once the aligning frames are presented
    ADVANCE = "advance"  # paused: the Frame Advances after it
    WAIT = "wait"  # the PNG


@dataclass
class ShotResult:
    path: Path | None
    source: Path | None
    frame: int | None
    paused: bool
    elapsed: float
    error: str | None = None


class ScreenshotJob:
    """Non-blocking Dolphin screenshot, driven by ``step()`` from a timer.

    ``before_advance`` is called before each of the ``SHOT_ADVANCES`` Frame Advances of
    the paused case (the UI rewrites the camera there). ``step()`` returns None while working,
    then a ``ShotResult``.
    """

    ALIGN_ADVANCES = 2  # before Take Screenshot: the camera written while paused reaches the picture
    ALIGN_WAIT = 0.25  # the aligning frames are presented before Take Screenshot is pressed

    def __init__(
        self,
        server: d.DSUServer,
        shots_dir: Path,
        dest_dir: Path,
        name: Callable[[int | None], str],
        counter: CounterFn,
        read_frame: CounterFn,
        before_advance: Callable[[], object] | None = None,
        timeout: float = SHOT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self.shots_dir = Path(shots_dir)
        self.dest_dir = Path(dest_dir)
        self.name = name
        self.counter = counter
        self.read_frame = read_frame
        self.before_advance = before_advance
        self.timeout = timeout
        self.clock = clock
        self.t0 = clock()
        self.state = ShotPhase.PROBE
        self.first = counter()
        self.paused = False
        self.advances = 0
        self.next_at = self.t0 + RUNNING_WINDOW
        self.before: dict[Path, int] = {}
        self.result: ShotResult | None = None

    def _done(self, path: Path | None, source: Path | None, frame: int | None, error: str | None) -> ShotResult:
        self.result = ShotResult(path, source, frame, self.paused, self.clock() - self.t0, error)
        return self.result

    def _shoot(self) -> ShotResult | None:
        self.before = snapshot(self.shots_dir)
        if not self.server.tap(d.SCREENSHOT):
            return self._done(None, None, None, "Dolphin does not listen to EBC's DSU pad")
        return None

    def step(self) -> ShotResult | None:
        if self.result is not None:
            return self.result
        handler = {
            ShotPhase.PROBE: self._probe,
            ShotPhase.ALIGN: self._align,
            ShotPhase.ALIGN_NEXT: self._align_next,
            ShotPhase.SHOOT: self._shoot_aligned,
            ShotPhase.ADVANCE: self._advance,
            ShotPhase.WAIT: self._wait,
        }[self.state]
        return handler(self.clock())

    def _probe(self, now: float) -> ShotResult | None:
        """Running or paused: the counter moves within RUNNING_WINDOW, or not."""
        current = self.counter()
        if current is not None and self.first is not None and current != self.first:
            self.paused = False  # it moves: running
        elif now < self.next_at:
            return None
        else:
            self.paused = current is not None and current == self.first
        if self.paused:
            self.state = ShotPhase.ALIGN
            return None
        failed = self._shoot()
        if failed is not None:
            return failed
        self.state = ShotPhase.WAIT
        return None

    def _align(self, now: float) -> ShotResult | None:
        """Paused: Frame Advance (camera rewritten first) until the frozen camera is in the picture."""
        if self.before_advance is not None:
            self.before_advance()
        if not self.server.tap(d.FRAME_ADVANCE):
            return self._done(None, None, None, "Dolphin does not listen to EBC's DSU pad")
        self.advances += 1
        if self.advances < self.ALIGN_ADVANCES:
            self.state = ShotPhase.ALIGN_NEXT
            self.next_at = now + d.HOLD + d.GAP + 0.05
        else:
            self.state = ShotPhase.SHOOT
            self.next_at = now + self.ALIGN_WAIT
        return None

    def _align_next(self, now: float) -> ShotResult | None:
        if now >= self.next_at:
            self.state = ShotPhase.ALIGN
        return None

    def _shoot_aligned(self, now: float) -> ShotResult | None:
        if now < self.next_at:
            return None
        failed = self._shoot()
        if failed is not None:
            return failed
        self.state = ShotPhase.ADVANCE
        self.next_at = now + d.HOLD + d.GAP
        return None

    def _advance(self, now: float) -> ShotResult | None:
        """Paused, after Take Screenshot: the remaining Frame Advances the capture needs."""
        if now < self.next_at:
            return None
        if self.before_advance is not None:
            self.before_advance()
        if not self.server.tap(d.FRAME_ADVANCE):
            return self._done(None, None, None, "Dolphin does not listen to EBC's DSU pad")
        self.advances += 1
        self.next_at = now + d.HOLD + d.GAP
        if self.advances >= SHOT_ADVANCES:
            self.state = ShotPhase.WAIT
        return None

    def _wait(self, now: float) -> ShotResult | None:
        """The new PNG, complete: moved to the destination under its EBC name."""
        new = [p for p in snapshot(self.shots_dir) if p not in self.before]
        done = [p for p in new if png_complete(p)]
        if done:
            src = max(done, key=lambda p: p.stat().st_mtime_ns)
            frame = self.read_frame()
            try:
                self.dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_path(self.dest_dir, self.name(frame))
                shutil.move(str(src), str(dest))
            except OSError as exc:
                return self._done(None, src, frame, f"could not move {src} to {self.dest_dir}: {exc}")
            return self._done(dest, src, frame, None)
        if now - self.t0 > self.timeout:
            return self._done(
                None,
                None,
                None,
                f"no new screenshot in {self.shots_dir} after {self.timeout:.0f} s "
                "(Take Screenshot hotkey bound? Hotkeys without window focus?)",
            )
        return None
