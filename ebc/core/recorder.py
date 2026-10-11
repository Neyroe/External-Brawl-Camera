"""Record the game camera, one sample per game frame. No ``bpy``: runs in a worker thread.

Two modes (docs/research/camera-motion.md 8):

* **Live**: the game runs at full speed. The worker polls the frame counter
  (``0x8062B420``) in a tight loop; the camera of frame N is computed 0.2 to
  4.8 ms after the counter moves (up to 8.7 ms measured on a loaded machine),
  so the gfCamera block is read once it has
  changed and is stable, at least ``min_delay`` after the counter. A heavy
  frame can publish its camera 15 ms late (measured on a KO), so there is no
  fixed timeout: a block that does not change before the next frame starts is a
  still camera. Frames the worker could not catch are counted as missed.
* **Frame by frame** (DevServer): pause, then for each frame resume until the
  counter moves, wait ``settle``, pause, read. Measured identical to the bit
  to a continuous run on a replay (120/120 frames).
* **Frame by frame** (official Dolphin, DSU hotkeys, ``FrameAdvanceRecorder``):
  Frame Advance pauses a running game without advancing, then each press runs
  exactly one frame and pauses (docs/research/screenshot.md 1.5); the block is
  read once the counter moved and the block is stable.

The worker never touches Blender: samples are queued and the UI drains them
from a ``bpy.app.timers`` callback on the main thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .camera import CameraSample, parse_camera_block

if TYPE_CHECKING:
    from .game import Game

ReadFn = Callable[[int, int], "bytes | None"]


@dataclass
class RecordStats:
    captured: int = 0
    missed: int = 0  # frames skipped between two captured ones
    repeated: int = 0  # frames seen again (the replay or a savestate went back)
    first: int | None = None
    last: int | None = None
    error: str | None = None
    missed_frames: list[int] = field(default_factory=list)  # first ones only, for the UI / tests

    def add(self, frame: int) -> None:
        if self.last is not None:
            if frame > self.last + 1:
                gap = range(self.last + 1, frame)
                self.missed += len(gap)
                room = 64 - len(self.missed_frames)
                if room > 0:
                    self.missed_frames.extend(list(gap)[:room])
            elif frame <= self.last:
                self.repeated += 1
        if self.first is None:
            self.first = frame
        self.last = frame
        self.captured += 1


class Worker:
    """Shared thread plumbing: start/stop, sample queue, stats."""

    def __init__(
        self,
        read: ReadFn,
        frame_addr: int,
        block_addr: int,
        block_size: int,
        end_frame: int = 0,
        *,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.read = read
        self.clock = clock  # injected by the tests (a simulated game)
        self.sleep = sleep
        self.frame_addr = frame_addr
        self.block_addr = block_addr
        self.block_size = block_size
        self.end_frame = end_frame
        self.stats = RecordStats()
        self.queue: deque[CameraSample] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def for_game(cls, game: Game, *args, **kwargs):
        """This recorder on ``game``'s frame counter and gfCamera block (read with ``game.read_bytes``)."""
        p = game.p
        return cls(game.read_bytes, p.MATCH_FRAME.addr, p.CAM_RECORD_BASE, p.CAM_RECORD_SIZE, *args, **kwargs)

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._safe_run, name="ebc-record", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def drain(self) -> list[CameraSample]:
        out = []
        while self.queue:
            out.append(self.queue.popleft())
        return out

    def _safe_run(self) -> None:
        try:
            self.run()
        except Exception as exc:  # never die silently: the UI shows it  # noqa: BLE001
            self.stats.error = f"recording stopped: {exc!r}"
        finally:
            self.finish()

    def finish(self) -> None:
        """Called on the worker thread when the loop ends (override)."""

    def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- reads ---------------------------------------------------------------------

    def read_frame(self) -> int | None:
        data = self.read(self.frame_addr, 4)
        return None if data is None else int.from_bytes(data, "big")

    def read_block(self) -> bytes | None:
        return self.read(self.block_addr, self.block_size)

    def push(self, frame: int, block: bytes) -> bool:
        sample = parse_camera_block(frame, block)
        if sample is None:
            return False
        self.queue.append(sample)
        self.stats.add(frame)
        return True

    def reached_end(self, frame: int) -> bool:
        return self.end_frame > 0 and frame >= self.end_frame

    def stable_block(self, gap: float) -> bytes | None:
        """The camera block once two reads ``gap`` apart agree (or the last one read on stop),
        None when it cannot be read."""
        block = self.read_block()
        while block is not None and not self._stop.is_set():
            self.sleep(gap)
            again = self.read_block()
            if again == block:
                break
            block = again
        return block

    def wait(self, seconds: float, step: float = 0.005) -> None:
        """Sleep ``seconds`` in steps, ending early on stop."""
        end = self.clock() + seconds
        while not self._stop.is_set() and self.clock() < end:
            self.sleep(step)


class LiveRecorder(Worker):
    """Full-speed recording by polling the frame counter (no pause)."""

    def __init__(
        self,
        read: ReadFn,
        frame_addr: int,
        block_addr: int,
        block_size: int,
        end_frame: int = 0,
        *,
        min_delay: float = 0.002,
        settle_timeout: float | None = None,
        poll: float = 0.0002,
        max_errors: int = 200,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(read, frame_addr, block_addr, block_size, end_frame, clock=clock, sleep=sleep)
        self.min_delay = min_delay
        self.settle_timeout = settle_timeout
        self.poll = poll
        self.max_errors = max_errors

    def run(self) -> None:
        last_frame: int | None = None
        errors = 0
        while not self._stop.is_set():
            frame = self.read_frame()
            if frame is None:
                errors += 1
                if errors >= self.max_errors:
                    self.stats.error = "the game frame counter cannot be read"
                    return
                self.sleep(0.01)
                continue
            errors = 0
            if frame != last_frame:
                # The first value seen may have changed long ago (or just before its camera
                # update): wait for the next change to know the phase.
                first = last_frame is None
                last_frame = frame
                if not first and frame > 0:
                    # Read right at the counter change, the block still shows the previous
                    # frame's camera (the update comes >= 0.2 ms later): wait for it to change.
                    reference = self.read_block()
                    block = self._capture(frame, reference)
                    if block is not None and self.push(frame, block) and self.reached_end(frame):
                        return
            self.sleep(self.poll)

    def _capture(self, frame: int, reference: bytes | None) -> bytes | None:
        """The camera block of ``frame``: once it changed from ``reference`` and is stable.

        No fixed timeout: a heavy frame (a KO, measured) can publish its camera
        15 ms after the counter. If the counter moves again while the block
        never changed, the camera stood still during ``frame`` and ``reference``
        is its camera. None only when the block cannot be read.
        """
        t0 = self.clock()
        while self.clock() - t0 < self.min_delay:
            self.sleep(self.poll)
        deadline = None if self.settle_timeout is None else t0 + self.settle_timeout
        while not self._stop.is_set():
            block = self.read_block()
            if block is None:
                return None
            if block != reference:
                self.sleep(self.poll)
                again = self.read_block()
                if again == block:
                    return block
                continue  # caught mid-update: read again
            if self.read_frame() != frame or (deadline is not None and self.clock() >= deadline):
                return reference  # still camera: nothing changed during the whole frame
            self.sleep(self.poll)
        return None


class Stepper(Protocol):
    """What frame-by-frame recording needs from the DevServer (pause / resume)."""

    def pause(self) -> bool: ...

    def resume(self) -> bool: ...


class FrameStepRecorder(Worker):
    """Pause, then resume -> counter moves -> camera updated -> pause -> read, frame after frame.

    "Camera updated" = the gfCamera block changed from what it was when the
    counter moved, or ``settle`` passed (a still camera). Measured: a fixed
    4 ms wait sometimes paused before the update (2 replay frames out of 120
    then held the previous frame's camera), and on a loaded machine the update
    came up to 8.7 ms after the counter: ``settle`` is 12 ms.
    """

    def __init__(
        self,
        read: ReadFn,
        frame_addr: int,
        block_addr: int,
        block_size: int,
        stepper: Stepper,
        end_frame: int = 0,
        *,
        settle: float = 0.012,
        min_delay: float = 0.001,
        frame_timeout: float = 0.5,
        resume_at_end: bool = True,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(read, frame_addr, block_addr, block_size, end_frame, clock=clock, sleep=sleep)
        self.stepper = stepper
        self.settle = settle
        self.min_delay = min_delay
        self.frame_timeout = frame_timeout
        self.resume_at_end = resume_at_end

    def run(self) -> None:
        if not self.stepper.pause():
            self.stats.error = "DevServer: pause failed"
            return
        while not self._stop.is_set():
            start = self.read_frame()
            if start is None:
                self.stats.error = "the game frame counter cannot be read"
                return
            if not self.stepper.resume():
                self.stats.error = "DevServer: resume failed"
                return
            deadline = self.clock() + self.frame_timeout
            frame = start
            while frame == start and self.clock() < deadline:
                self.sleep(0.0002)
                frame = self.read_frame()
            if frame is None or frame == start:
                self.stepper.pause()
                self.stats.error = "the game frame did not move (paused in game, or not in a match?)"
                return
            t0 = self.clock()
            reference = self.read_block()
            while self.clock() - t0 < self.settle:
                self.sleep(0.0002)
                if self.clock() - t0 >= self.min_delay and self.read_block() != reference:
                    self.sleep(0.0003)  # let the update finish (it takes microseconds)
                    break
            if not self.stepper.pause():
                self.stats.error = "DevServer: pause failed"
                return
            frame = self.read_frame()
            block = self.read_block()
            if frame is None or block is None:
                self.stats.error = "camera read failed"
                return
            self.push(frame, block)
            if self.reached_end(frame):
                return

    def finish(self) -> None:
        if self.resume_at_end:
            self.stepper.resume()


class FrameAdvancer(Protocol):
    """What frame-by-frame recording needs from Dolphin's hotkeys (``emucontrol.DSUControl``)."""

    def is_running(self) -> bool | None: ...

    def advance_frame(self) -> bool: ...

    def toggle_pause(self) -> bool: ...


class FrameAdvanceRecorder(Worker):
    """Frame Advance, counter moved, block stable -> read, frame after frame (no DevServer).

    The emulator is paused first (a Frame Advance while running pauses without
    advancing). Each press runs exactly one frame and pauses again, so the
    camera of the new frame is complete and does not change: it is read once
    two reads ``settle`` apart agree. The game is resumed at the end if it was
    running at the start.

    Measured on the P+ 3.2 AppImage: right after a savestate load, the press that
    follows the first step can be dropped by Dolphin (the step is not finished
    although the counter moved): a press that does not move the counter within
    ``frame_timeout`` is pressed again (``retries``); a double step would show
    in ``not_one``.
    """

    def __init__(
        self,
        read: ReadFn,
        frame_addr: int,
        block_addr: int,
        block_size: int,
        advancer: FrameAdvancer,
        end_frame: int = 0,
        *,
        settle: float = 0.004,
        paused_for: float = 0.03,
        first_settle: float = 0.4,
        frame_timeout: float = 0.3,
        attempts: int = 4,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(read, frame_addr, block_addr, block_size, end_frame, clock=clock, sleep=sleep)
        self.advancer = advancer
        self.settle = settle
        self.paused_for = paused_for
        self.first_settle = first_settle
        self.frame_timeout = frame_timeout
        self.attempts = attempts
        self.retries = 0  # presses Dolphin dropped and that were pressed again
        self.was_running = False
        self.steps = 0
        self.not_one = 0  # presses that did not move the counter by exactly one frame

    def _wait_frame(self, start: int) -> int | None:
        deadline = self.clock() + self.frame_timeout
        frame = start
        while frame == start and self.clock() < deadline and not self._stop.is_set():
            self.sleep(0.001)
            frame = self.read_frame()
            if frame is None:
                return None
        return frame

    def run(self) -> None:
        if not self._pause_first():
            return
        while not self._stop.is_set():
            start = self.read_frame()
            if start is None:
                self.stats.error = "the game frame counter cannot be read"
                return
            frame = self._advance_one(start)
            if frame is None:
                return
            if not self.stats.captured:
                # The first step after a pause or a savestate load can be slow (measured: Dolphin
                # still running ~0.1-0.3 s after the counter moved, a press then is dropped and the
                # camera of that frame is not written yet): let it finish before reading.
                self.wait(self.first_settle)
            if frame != start + 1 and self.stats.captured:
                # (the first step after a pause taken mid-frame may be +2: Dolphin stops a step at
                # the first field boundary after a NEW frame was presented, Core::Callback_NewField)
                self.not_one += 1
            block = self.stable_block(self.settle)
            if block is None:
                self.stats.error = "camera read failed"
                return
            self.push(frame, block)
            if self.reached_end(frame):
                return
            self._settle_paused(frame)

    def _pause_first(self) -> bool:
        """Pause a running game (a Frame Advance pauses without advancing). False on an error."""
        running = self.advancer.is_running()
        if running is None:
            self.stats.error = "the game frame counter cannot be read"
            return False
        self.was_running = running
        if running:
            if not self.advancer.advance_frame():  # pauses without advancing
                self.stats.error = "Frame Advance hotkey failed (Dolphin listening to EBC?)"
                return False
            if self.advancer.is_running():
                self.stats.error = "Dolphin did not pause (Frame Advance hotkey bound?)"
                return False
        return True

    def _advance_one(self, start: int) -> int | None:
        """One Frame Advance (pressed again when Dolphin drops it): the new frame, None to stop
        (the error set, unless stopped)."""
        frame: int | None = start
        for _attempt in range(self.attempts):
            if not self.advancer.advance_frame():
                self.stats.error = "Frame Advance hotkey failed"
                return None
            self.steps += 1
            frame = self._wait_frame(start)
            if frame != start or self._stop.is_set():
                break
            self.retries += 1  # dropped by Dolphin (still finishing the previous step): again
        if frame is None:
            self.stats.error = "the game frame counter cannot be read"
            return None
        if frame == start:
            if not self._stop.is_set():
                self.stats.error = "the game frame did not move (not in a match, or Frame Advance not bound?)"
            return None
        return frame

    def _settle_paused(self, frame: int) -> None:
        """Wait until the counter has held still for ``paused_for`` (more than a frame).

        Measured on the P+ 3.2 AppImage: a Frame Advance pressed as soon as the counter
        moved is ignored (Dolphin is still finishing the previous step and has not paused
        yet; ``Core::DoFrameStep`` drops it), and the recording stalls.
        """
        last, since = frame, self.clock()
        deadline = since + self.frame_timeout
        while not self._stop.is_set() and self.clock() < deadline:
            if self.clock() - since >= self.paused_for:
                return
            self.sleep(0.002)
            now = self.read_frame()
            if now != last:
                last, since = now, self.clock()

    def finish(self) -> None:
        if self.was_running:
            self.advancer.toggle_pause()
