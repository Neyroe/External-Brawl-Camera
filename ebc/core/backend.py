"""Memory backends: the only code allowed to touch the emulator's memory.

``core`` never imports ``bpy``. Every read and write goes through a
``MemoryBackend``; the high level API (``game.Game``) turns backend failures
into ``None``/``False`` so nothing ever raises inside a Blender ``update=``
callback.

Addresses are absolute guest addresses (``0x80xxxxxx`` for MEM1,
``0x90xxxxxx`` for MEM2), exactly as Dolphin Memory Engine shows them.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

PROCESS_NAME_ENV = "DME_DOLPHIN_PROCESS_NAME"
# Names DME matches on Linux when the variable is unset (exact /proc/<pid>/comm).
DME_DEFAULT_NAMES = ("dolphin-emu", "dolphin-emu-qt2", "dolphin-emu-wx")
# Other Dolphin builds EBC knows: the Project+ 3.2 AppImage runs ``project-plus-dolphin``
# (comm is truncated to 15 characters).
KNOWN_NAMES = (*DME_DEFAULT_NAMES, "project-plus-do")


def running_process_names(proc: str = "/proc") -> list[str] | None:
    """comm of every running process (Linux), None where /proc does not exist."""
    if not os.path.isdir(proc):
        return None
    names = []
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/comm", encoding="utf-8", errors="replace") as f:
                names.append(f.read().strip())
        except OSError:
            continue
    return names


def choose_process_name(env: str | None, running: Sequence[str]) -> tuple[str | None, bool, str | None]:
    """Decide the DME process name before its first lookup (DME reads the variable once).

    Returns ``(name_to_set, can_hook, notice)``. ``name_to_set`` is None when the
    environment is fine as it is. ``can_hook`` is False when no Dolphin runs: DME is
    then not called at all, so the choice stays open for the next Connect.
    """
    if env and env in running:
        return None, True, None
    found = [n for n in KNOWN_NAMES if n in running]
    if not found:
        if env:
            return None, False, f"No Dolphin process named {env!r} ({PROCESS_NAME_ENV}) or {', '.join(KNOWN_NAMES)}"
        return None, False, "No Dolphin process found. Is the game running?"
    pick = found[0]
    notice = f"Several Dolphin builds running ({', '.join(found)}): using {pick!r}" if len(found) > 1 else None
    if env:
        notice = f"{PROCESS_NAME_ENV}={env!r} matches no process: using {pick!r}"
        return pick, True, notice
    if pick in DME_DEFAULT_NAMES:
        return None, True, notice
    return pick, True, notice


class BackendError(RuntimeError):
    """A read or write could not be performed (unhooked, bad address, ...)."""


@runtime_checkable
class MemoryBackend(Protocol):
    def hook(self) -> bool:
        """Try to attach to the emulator. Return the new hooked state."""
        ...

    def unhook(self) -> None: ...

    def is_hooked(self) -> bool: ...

    def read(self, addr: int, n: int) -> bytes:
        """Read ``n`` bytes at ``addr``. Raise ``BackendError`` on failure."""
        ...

    def write(self, addr: int, data: bytes) -> None:
        """Write ``data`` at ``addr``. Raise ``BackendError`` on failure."""
        ...


class DMEBackend:
    """Backend over the ``dolphin_memory_engine`` wheel bundled in the extension.

    The module is imported lazily so that ``core`` stays importable (and
    testable) on a machine without the wheel.
    """

    def __init__(self) -> None:
        self._dme = None
        self.import_error: str | None = None
        self.notice: str | None = None  # last word on process selection, for the UI
        # Name DME captured on its first lookup (it reads the variable only once per
        # process); "" = variable unset, defaults matched. None = not looked up yet.
        self.bound_name: str | None = None

    def _prepare_process_name(self) -> bool:
        """Set the variable before DME's first lookup; False when there is nothing to hook."""
        running = running_process_names()
        if running is None:  # Windows / macOS: DME's own lookup, no /proc to look at
            self.bound_name = self.bound_name if self.bound_name is not None else os.environ.get(PROCESS_NAME_ENV, "")
            return True
        if self.bound_name is not None:
            names = (self.bound_name,) if self.bound_name else DME_DEFAULT_NAMES
            if not any(n in running for n in names):
                others = [n for n in KNOWN_NAMES if n in running and n not in names]
                bound = repr(self.bound_name) if self.bound_name else "the default names"
                self.notice = (
                    f"Dolphin memory engine is bound to {bound} for this Blender session: "
                    f"restart Blender to connect to {others[0]!r}"
                    if others
                    else "No Dolphin process found. Is the game running?"
                )
                return False
            return True
        name, ok, self.notice = choose_process_name(os.environ.get(PROCESS_NAME_ENV), running)
        if not ok:
            return False
        if name is not None:
            os.environ[PROCESS_NAME_ENV] = name
        self.bound_name = os.environ.get(PROCESS_NAME_ENV, "")
        return True

    def _module(self):
        if self._dme is None:
            try:
                import dolphin_memory_engine  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - depends on the install
                self.import_error = str(exc)
                raise BackendError(f"dolphin_memory_engine is not available: {exc}") from exc
            self._dme = dolphin_memory_engine
        return self._dme

    def hook(self) -> bool:
        try:
            dme = self._module()
            if not dme.is_hooked():
                if not self._prepare_process_name():
                    return False
                dme.hook()
            return bool(dme.is_hooked())
        except BackendError:
            return False
        except Exception:  # DME raises bare RuntimeError on some platforms
            return False

    def unhook(self) -> None:
        if self._dme is None:
            return
        try:
            self._dme.un_hook()
        except Exception:
            pass

    def is_hooked(self) -> bool:
        if self._dme is None:
            return False
        try:
            return bool(self._dme.is_hooked())
        except Exception:
            return False

    def read(self, addr: int, n: int) -> bytes:
        dme = self._module()
        try:
            data = dme.read_bytes(addr, n)
        except Exception as exc:
            # DME does not notice that Dolphin went away: a failed read is the
            # signal. Unhook so the UI shows "disconnected" instead of retrying
            # 60 times per second.
            self.unhook()
            raise BackendError(f"read 0x{addr:08X} ({n} bytes) failed: {exc}") from exc
        if data is None or len(data) != n:
            raise BackendError(f"short read at 0x{addr:08X}")
        return bytes(data)

    def write(self, addr: int, data: bytes) -> None:
        dme = self._module()
        try:
            dme.write_bytes(addr, bytes(data))
        except Exception as exc:
            self.unhook()
            raise BackendError(f"write 0x{addr:08X} ({len(data)} bytes) failed: {exc}") from exc


class FakeBackend:
    """In-memory backend for tests and the headless smoke test.

    Unset bytes read as zero. ``writes`` keeps every write in order, so tests
    can assert on the exact encoding sent to the game.
    """

    def __init__(self, hooked: bool = True, memory: dict[int, int] | None = None) -> None:
        self.hooked = hooked
        self.can_hook = True
        self.memory: dict[int, int] = dict(memory or {})
        self.writes: list[tuple[int, bytes]] = []
        self.reads: list[tuple[int, int]] = []
        self.fail_addresses: set[int] = set()

    # -- MemoryBackend ------------------------------------------------------

    def hook(self) -> bool:
        if self.can_hook:
            self.hooked = True
        return self.hooked

    def unhook(self) -> None:
        self.hooked = False

    def is_hooked(self) -> bool:
        return self.hooked

    def read(self, addr: int, n: int) -> bytes:
        if not self.hooked:
            raise BackendError("not hooked")
        if addr in self.fail_addresses:
            raise BackendError(f"read 0x{addr:08X} failed")
        self.reads.append((addr, n))
        return bytes(self.memory.get(addr + i, 0) for i in range(n))

    def write(self, addr: int, data: bytes) -> None:
        if not self.hooked:
            raise BackendError("not hooked")
        if addr in self.fail_addresses:
            raise BackendError(f"write 0x{addr:08X} failed")
        self.writes.append((addr, bytes(data)))
        self.poke(addr, data)

    # -- test helpers -------------------------------------------------------

    def poke(self, addr: int, data: bytes) -> None:
        """Set memory without recording a write (simulates the game)."""
        for i, b in enumerate(data):
            self.memory[addr + i] = b

    def peek(self, addr: int, n: int) -> bytes:
        return bytes(self.memory.get(addr + i, 0) for i in range(n))

    def writes_at(self, addr: int) -> list[bytes]:
        return [data for a, data in self.writes if a == addr]

    def clear_log(self) -> None:
        self.writes.clear()
        self.reads.clear()
