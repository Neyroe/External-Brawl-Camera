"""Optional client for the Dolphin DevServer (custom Dolphin build).

Protocol: TCP on 127.0.0.1, one text command per line, one answer per line,
``ok [payload]`` or ``err <message>``. Only the commands EBC needs are
wrapped: ``status``, ``pause``, ``resume``, ``step`` and ``state save|load``.

Everything degrades quietly: if the port is closed every call returns
``None``/``False`` within ``timeout`` seconds and ``last_error`` says why.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 27100
DEFAULT_TIMEOUT = 0.3


class DevServerError(RuntimeError):
    pass


@dataclass
class DevServerStatus:
    running: bool
    stepping: bool
    raw: dict[str, int | str]


def parse_status(payload: str) -> DevServerStatus:
    """``running=1 stepping=0 pc=0x801e0bcc lr=...`` -> DevServerStatus."""
    raw: dict[str, int | str] = {}
    for token in payload.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        try:
            raw[key] = int(value, 0)
        except ValueError:
            raw[key] = value
    return DevServerStatus(running=raw.get("running") == 1, stepping=raw.get("stepping") == 1, raw=raw)


class DevServerClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = b""
        self.last_error: str | None = None
        self.available = False  # last known reachability, for the UI (no I/O in draw())
        # One command at a time: the frame-by-frame recorder runs on a worker thread.
        self._lock = threading.RLock()

    # -- connection ------------------------------------------------------------

    def _connect(self) -> socket.socket:
        if self._sock is None:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            self._sock = sock
            self._buffer = b""
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""

    def set_port(self, port: int) -> None:
        if port != self.port:
            self.close()
            self.port = port
            self.available = False

    def _readline(self, sock: socket.socket) -> str:
        while b"\n" not in self._buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise DevServerError("connection closed by the DevServer")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8", "replace").rstrip("\r")

    def command(self, line: str) -> str:
        """Send one command, return the ``ok`` payload. Raise DevServerError."""
        with self._lock:
            return self._command(line)

    def _command(self, line: str) -> str:
        try:
            sock = self._connect()
            sock.sendall(line.encode("utf-8") + b"\n")
            answer = self._readline(sock)
        except (OSError, DevServerError) as exc:
            self.close()
            self.available = False
            raise DevServerError(f"{line!r}: {exc}") from exc
        self.available = True
        if answer == "ok" or answer.startswith("ok "):
            return answer[3:]
        if answer.startswith("err"):
            raise DevServerError(f"{line!r} -> {answer}")
        raise DevServerError(f"{line!r}: unexpected answer {answer!r}")

    def _try(self, line: str) -> str | None:
        try:
            payload = self.command(line)
        except DevServerError as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return payload

    # -- commands ----------------------------------------------------------------

    def status(self) -> DevServerStatus | None:
        payload = self._try("status")
        return None if payload is None else parse_status(payload)

    def pause(self) -> bool:
        return self._try("pause") is not None

    def resume(self) -> bool:
        return self._try("resume") is not None

    def step(self) -> bool:
        """ONE CPU INSTRUCTION (the DevServer has no frame advance). See advance_frame()."""
        return self._try("step") is not None

    def advance_frame(self, read_frame: Callable[[], int | None], timeout: float = 0.5) -> bool:
        """Run until the game frame counter changes, then pause (approximate frame advance).

        ``read_frame`` reads the game's frame counter (through DME). The pause
        lands within the next frame or two; outside a match the counter does
        not move and the emulator is paused again after ``timeout``.
        """
        start = read_frame()
        if start is None or not self.resume():
            return False
        deadline = time.monotonic() + timeout
        moved = False
        while time.monotonic() < deadline:
            frame = read_frame()
            if frame is not None and frame != start:
                moved = True
                break
            time.sleep(0.001)
        paused = self.pause()
        if not moved:
            self.last_error = "the game frame did not change (not in a match?)"
        return paused and moved

    def save_state(self, path: str) -> bool:
        return self._try(f"state save {path}") is not None

    def load_state(self, path: str) -> bool:
        return self._try(f"state load {path}") is not None
