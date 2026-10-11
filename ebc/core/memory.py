"""Memory access with a call budget (docs/DEVELOPMENT.md "Memory access budget"). No ``bpy``.

``MemorySession`` wraps a ``MemoryBackend``: every access is counted (``IOStats``) and never raises
(``None`` / ``False``, ``last_error``). Inside ``with session.tick():`` (the memory phase of one sync
tick, on the thread that created the session) reads are served from a snapshot read in a few
merged calls, and writes offered every tick are skipped when the snapshot already holds them.
``Game`` is a ``MemorySession``.
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from .addresses import MEM1_END, MEM1_START, MEM2_END, MEM2_START, Field, is_valid_pointer
from .backend import BackendError, MemoryBackend

# -- Memory access budget (docs/DEVELOPMENT.md "Memory access budget") ----------
#
# One DME call costs about 2 us whatever its size up to 16 KiB (measured, see the guide): two
# reads closer than MERGE_GAP are cheaper as one.
MERGE_GAP = 4096
MAX_SPAN = 16384


@dataclass
class IOStats:
    """Backend calls made by the owner thread (the one that created the Game)."""

    reads: int = 0
    writes: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    skipped: int = 0  # writes not sent: this tick's snapshot already held exactly those bytes
    hits: int = 0  # reads served by this tick's snapshot
    ns: int = 0  # time spent inside backend calls

    @property
    def calls(self) -> int:
        return self.reads + self.writes

    def copy(self) -> IOStats:
        return IOStats(self.reads, self.writes, self.bytes_read, self.bytes_written, self.skipped, self.hits, self.ns)

    def since(self, before: IOStats) -> IOStats:
        return IOStats(
            self.reads - before.reads,
            self.writes - before.writes,
            self.bytes_read - before.bytes_read,
            self.bytes_written - before.bytes_written,
            self.skipped - before.skipped,
            self.hits - before.hits,
            self.ns - before.ns,
        )


def _region(addr: int) -> int | None:
    if MEM1_START <= addr < MEM1_END:
        return 1
    if MEM2_START <= addr < MEM2_END:
        return 2
    return None


def plan_reads(ranges: Sequence[tuple[int, int]], gap: int = MERGE_GAP, span: int = MAX_SPAN) -> list[tuple[int, int]]:
    """Merge ``(addr, size)`` ranges into as few reads as possible.

    Two ranges are read together when the hole between them is at most ``gap`` bytes and the
    whole read stays within ``span`` bytes and one memory region (MEM1 or MEM2): only bytes the
    game owns are read, nothing is written. Ranges outside MEM1/MEM2 are dropped.
    """
    clean = sorted(
        (a, a + n) for a, n in set(ranges) if n > 0 and _region(a) is not None and _region(a) == _region(a + n - 1)
    )
    out: list[list[int]] = []
    for start, end in clean:
        if out:
            cur = out[-1]
            if start - cur[1] <= gap and max(end, cur[1]) - cur[0] <= span and _region(start) == _region(cur[0]):
                cur[1] = max(cur[1], end)
                continue
        out.append([start, end])
    return [(a, b - a) for a, b in out]


class _Snapshot:
    """Bytes read during one tick (memory as it was then), kept up to date with our own writes."""

    def __init__(self) -> None:
        self.blocks: list[tuple[int, bytearray]] = []

    def get(self, addr: int, n: int) -> bytes | None:
        for start, buf in self.blocks:
            off = addr - start
            if off >= 0 and off + n <= len(buf):
                return bytes(buf[off : off + n])
        return None

    def patch(self, addr: int, data: bytes) -> None:
        """Newer bytes (our write, a fresh read): update every copy of them."""
        end = addr + len(data)
        for start, buf in self.blocks:
            lo, hi = max(addr, start), min(end, start + len(buf))
            if lo < hi:
                buf[lo - start : hi - start] = data[lo - addr : hi - addr]

    def put(self, addr: int, data: bytes) -> None:
        self.patch(addr, data)
        self.blocks.append((addr, bytearray(data)))


class MemorySession:
    def __init__(self, backend: MemoryBackend) -> None:
        self.backend = backend
        self.last_error: str | None = None
        # call counters, per-tick snapshot (see ``tick``)
        self._owner = threading.get_ident()
        self.io = IOStats()
        self.tick_serial = 0  # ticks opened so far: "already done in this tick" checks compare it
        self._snap: _Snapshot | None = None
        self._read_set: list[tuple[int, int]] = []  # ranges read in the current tick
        self._prefetch_set: list[tuple[int, int]] = []  # ... in the previous one: read first, merged
        self._wanted: dict[int, bytes] = {}  # skip_same writes asked in the current tick
        self._last_wanted: dict[int, bytes] = {}
        self._plan: tuple[frozenset, list[tuple[int, int]]] = (frozenset(), [])
        self._tick_hooked = False  # backend hooked when the tick opened, and no call failed since

    @property
    def connected(self) -> bool:
        if self._tick_hooked and self._snap is not None and threading.get_ident() == self._owner:
            return True  # checked when the tick opened; a failed call ends it (_tick_hooked)
        try:
            return bool(self.backend.is_hooked())
        except Exception:  # the backend (DME) may raise anything: not hooked then  # noqa: BLE001
            return False

    # -- tick snapshot ----------------------------------------------------------------
    #
    # Inside ``with game.tick():`` (the memory phase of one sync tick, owner thread only):
    #
    # * the ranges read by the previous tick are read again first, merged by ``plan_reads``
    #   (pointer chains rarely move: their next hops are then already in the snapshot; a hop
    #   that moved is a miss, read on its own). Every pointer is still checked by the code
    #   that follows it: the snapshot holds real bytes of this tick, nothing is guessed;
    # * reads are served from the snapshot when it covers them;
    # * ``write_bytes(..., skip_same=True)`` sends nothing when the snapshot already holds
    #   exactly those bytes (the game has them now: writing them again changes nothing).
    #   The range of a write repeated with the same bytes is read with the next snapshot,
    #   so a still camera / a kept tint costs no write at all, a moving one no extra read.
    #
    # Outside a tick (UI callbacks, the recorder thread, tools) every access goes straight
    # to the backend.

    @contextmanager
    def tick(self) -> Iterator[IOStats]:
        """One sync tick's memory phase: snapshot reads, skipped writes. Not re-entrant."""
        if self._snap is not None or threading.get_ident() != self._owner:
            yield self.io
            return
        self._snap = _Snapshot()
        self._read_set = []
        self._wanted = {}
        self.tick_serial += 1
        self._tick_hooked = False
        try:
            self._tick_hooked = self.connected
            if self._tick_hooked:
                key = frozenset(self._prefetch_set)
                if self._plan[0] != key:  # the same ranges tick after tick: plan once
                    self._plan = (key, plan_reads(self._prefetch_set))
                for addr, n in self._plan[1]:
                    data = self._backend_read(addr, n)
                    if data is None:
                        break  # disconnected or unreadable: the code below reads what it needs
                    self._snap.put(addr, data)
            yield self.io
        finally:
            self._prefetch_set = list(set(self._read_set))
            self._tick_hooked = False
            self._last_wanted = self._wanted
            self._snap = None
            self._read_set = []
            self._wanted = {}

    @property
    def in_tick(self) -> bool:
        """True inside ``tick()`` on the owner thread (reads served by the snapshot)."""
        return self._tick_snapshot() is not None

    def _tick_snapshot(self) -> _Snapshot | None:
        if self._snap is None or threading.get_ident() != self._owner:
            return None
        return self._snap

    # -- raw access (never raises) -----------------------------------------------

    def _count(self, t0: int, reads: int, writes: int, nbytes: int) -> None:
        if threading.get_ident() != self._owner:
            return  # the recorder thread: not part of the sync budget
        io = self.io
        io.ns += time.perf_counter_ns() - t0
        io.reads += reads
        io.writes += writes
        if reads:
            io.bytes_read += nbytes
        else:
            io.bytes_written += nbytes

    def _backend_read(self, addr: int, n: int) -> bytes | None:
        t0 = time.perf_counter_ns()
        try:
            return self.backend.read(addr, n)
        except BackendError as exc:
            error = str(exc)
        except Exception as exc:  # the backend (DME) may raise anything: the call failed  # noqa: BLE001
            error = f"unexpected backend error: {exc}"
        finally:
            self._count(t0, 1, 0, n)
        self._failed(error)
        return None

    def _failed(self, error: str) -> None:
        """A backend call failed. Only the owner thread's state changes: the recorder worker reports
        its own errors (``RecordStats.error``) and must not end the sync tick's fast path."""
        if threading.get_ident() != self._owner:
            return
        self.last_error = error
        self._tick_hooked = False  # the backend may have unhooked: ask it again

    def read_bytes(self, addr: int, n: int, fresh: bool = False) -> bytes | None:
        """``n`` bytes at ``addr``, None if unreadable.

        In a tick, served from the snapshot unless ``fresh`` (read-modify-write of a byte the
        game may change at any time).
        """
        snap = self._snap
        if snap is not None and self._tick_hooked and threading.get_ident() == self._owner:
            if not fresh:  # fast path: inside the tick, hooked when it opened
                self._read_set.append((addr, n))
                for start, buf in snap.blocks:
                    off = addr - start
                    if off >= 0 and off + n <= len(buf):
                        self.io.hits += 1
                        return bytes(buf[off : off + n])
        elif not self.connected:
            return None
        else:
            snap = self._tick_snapshot()
        data = self._backend_read(addr, n)
        if data is not None and snap is not None:
            snap.put(addr, data)
        return data

    def write_bytes(self, addr: int, data: bytes, skip_same: bool = False) -> bool:
        """Write ``data`` at ``addr``; False if disconnected or it failed.

        ``skip_same`` (in a tick only): nothing is sent when this tick's snapshot shows the
        bytes are already there. Only for values written every tick.
        """
        if not self.connected:
            return False
        data = bytes(data)
        snap = self._tick_snapshot()
        if snap is not None and skip_same:
            if self._last_wanted.get(addr) == data:
                self._read_set.append((addr, len(data)))  # same value again: check it next tick
            self._wanted[addr] = data
            if snap.get(addr, len(data)) == data:
                self.io.skipped += 1
                return True
        t0 = time.perf_counter_ns()
        error = None
        try:
            self.backend.write(addr, data)
        except BackendError as exc:
            error = str(exc)
        except Exception as exc:  # the backend (DME) may raise anything: the call failed  # noqa: BLE001
            error = f"unexpected backend error: {exc}"
        finally:
            self._count(t0, 0, 1, len(data))
        if error is not None:
            self._failed(error)
            return False
        if snap is not None:
            snap.patch(addr, data)
        return True

    def snapshot_holds(self, addr: int, data: bytes) -> bool:
        """True in a tick when this tick's snapshot shows ``data`` at ``addr`` (no read is made)."""
        snap = self._tick_snapshot()
        if snap is None:
            return False
        self._read_set.append((addr, len(data)))
        return snap.get(addr, len(data)) == bytes(data)

    def read_field(self, field: Field) -> tuple | None:
        data = self.read_bytes(field.addr, field.size)
        return None if data is None else field.unpack(data)

    def read_value(self, field: Field):
        values = self.read_field(field)
        return None if values is None else values[0]

    def write_field(self, field: Field, *values: float | int, skip_same: bool = False) -> bool:
        return self.write_bytes(field.addr, field.pack(*values), skip_same=skip_same)

    def read_u32(self, addr: int) -> int | None:
        data = self.read_bytes(addr, 4)
        return None if data is None else struct.unpack(">I", data)[0]

    def read_value_at(self, addr: int, fmt: str):
        """One value of ``fmt`` at a dynamic address (a structure member), None if unreadable."""
        data = self.read_bytes(addr, struct.calcsize(fmt)) if is_valid_pointer(addr) else None
        return None if data is None else struct.unpack(fmt, data)[0]

    def scan(self, start: int, size: int, needle: bytes, chunk: int = 0x10000) -> Iterator[int]:
        """Addresses of ``needle`` (4-aligned) in ``[start, start + size)``, straight from the backend
        (never through a tick snapshot). Once per match at most: a 1 MiB window is 16 calls."""
        if not self.connected:
            return
        end = start + size
        addr = start
        overlap = len(needle) - 1
        while addr < end:
            n = min(chunk, end - addr)
            data = self._backend_read(addr, n + overlap if addr + n + overlap <= end else n)
            if data is None:
                return
            at = data.find(needle)
            while at != -1:
                if at < n and (addr + at) % 4 == 0:
                    yield addr + at
                at = data.find(needle, at + 1)
            addr += n

    def deref(self, addr: int) -> int | None:
        """Read a pointer at ``addr``; None unless both addresses are valid RAM."""
        if not is_valid_pointer(addr):
            return None
        ptr = self.read_u32(addr)
        if ptr is None or not is_valid_pointer(ptr):
            return None
        return ptr
