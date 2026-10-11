"""A one-pad DSU (cemuhook) server: Dolphin's hotkeys bound to its buttons. No ``bpy``.

Dolphin's DSU client (Controllers > Alternate Input Sources) polls this
server and sees one pad, ``DSUClient/0/EBC``. Its hotkeys (Take Screenshot,
Toggle Pause, Frame Advance, Save/Load State Slot 10) are bound to buttons of
that pad (``dolphin_config.install``), so pressing a virtual button fires the
native hotkey of the OFFICIAL Dolphin, without DevServer or custom build.

Report and measures: ``docs/research/screenshot.md`` (P+ 3.2 AppImage). Taken
from ``DSUPad`` in ``docs/research/tools/screenshot_probe.py``.

Dolphin's hotkey thread sleeps 5 ms, drains the socket keeping the LAST packet,
and fires on a rising edge: a press stays visible ``hold`` seconds (50 ms) and
is released before the next one. A server started after Dolphin is seen within
about one second (Dolphin asks for the pad list every second).
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import zlib

DEFAULT_PORT = 26790  # not 26760, the usual cemuhook port (real pad servers, wii_dsu.py)
HOST = "127.0.0.1"
DEVICE = "DSUClient/0/EBC"
SERVER_NAME = "EBC"

PROTOCOL_VERSION = 1001
HEADER = struct.Struct("<4sHHII")  # magic, version, payload length, crc32, sender id
MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_PAD_DATA = 0x100002
CLIENT_TIMEOUT = 5.0  # a client that stopped asking for pad data is forgotten

# name -> (field, value). "b1" = button_states1 bitfield, "ps" / "touch" = their own
# byte, "a" = analog byte index (0..11: dpad W S E N, Square Cross Circle Triangle,
# R1 L1 R2 L2), pressed = 255.
BUTTONS = {
    "Share": ("b1", 0x01),
    "L3": ("b1", 0x02),
    "R3": ("b1", 0x04),
    "Options": ("b1", 0x08),
    "PS": ("ps", 0x01),
    "Touch Button": ("touch", 0x01),
    "Pad W": ("a", 0),
    "Pad S": ("a", 1),
    "Pad E": ("a", 2),
    "Pad N": ("a", 3),
    "Square": ("a", 4),
    "Cross": ("a", 5),
    "Circle": ("a", 6),
    "Triangle": ("a", 7),
    "R1": ("a", 8),
    "L1": ("a", 9),
    "R2": ("a", 10),
    "L2": ("a", 11),
}
# Dolphin reads the analog bytes for the dpad and face buttons; real servers also set the
# bits of button_states1 (dpad, high nibble) and button_states2, so do we.
_B1_DPAD = {0: 0x80, 1: 0x40, 2: 0x20, 3: 0x10}
_B2 = {4: 0x80, 5: 0x40, 6: 0x20, 7: 0x10, 8: 0x08, 9: 0x04, 10: 0x02, 11: 0x01}

MAC = bytes([0x00, 0xEB, 0xC0, 0x00, 0x00, 0x01])

# The hotkeys EBC binds (Hotkeys.ini key -> button), see dolphin_config.install.
EBC_SLOT = 10  # savestate slot reserved for EBC (Dolphin's default keys only use 1..8)
SCREENSHOT = "Share"
TOGGLE_PAUSE = "Options"
FRAME_ADVANCE = "PS"
SAVE_SLOT = "L3"
LOAD_SLOT = "R3"
BINDINGS = {
    "General/Take Screenshot": SCREENSHOT,
    "General/Toggle Pause": TOGGLE_PAUSE,
    "Frame Advance/Frame Advance": FRAME_ADVANCE,
    f"Save State/Save State Slot {EBC_SLOT}": SAVE_SLOT,
    f"Load State/Load State Slot {EBC_SLOT}": LOAD_SLOT,
}

HOLD = 0.05  # seconds a press stays down (10 hotkey polls)
GAP = 0.03  # seconds released after a press, before the next one


def packet(uid: int, body: bytes) -> bytes:
    """Server packet: ``DSUS`` header (CRC32 over header with a zero CRC + body) + body."""
    head = HEADER.pack(b"DSUS", PROTOCOL_VERSION, len(body), 0, uid)
    crc = zlib.crc32(head + body) & 0xFFFFFFFF
    return HEADER.pack(b"DSUS", PROTOCOL_VERSION, len(body), crc, uid) + body


def version_body() -> bytes:
    return struct.pack("<IH2x", MSG_VERSION, PROTOCOL_VERSION)


def port_info_body(slot: int) -> bytes:
    """Answer to a port-info request: slot 0 is our pad, the others are absent."""
    state = 2 if slot == 0 else 0
    body = struct.pack("<IBBBB", MSG_PORTS, slot, state, 2 if state else 0, 2 if state else 0)
    return body + (MAC if state else bytes(6)) + struct.pack("<BB", 0x05 if state else 0, 0)


def pad_body(held: set[str] | frozenset[str], counter: int, timestamp_us: int) -> bytes:
    """Pad data of slot 0 with ``held`` buttons down (84 bytes: 100 with the header)."""
    b1 = b2 = ps = touch = 0
    analog = [0] * 12
    for name in held:
        kind, val = BUTTONS[name]
        if kind == "b1":
            b1 |= val
        elif kind == "ps":
            ps |= val
        elif kind == "touch":
            touch |= val
        else:
            analog[val] = 255
            b1 |= _B1_DPAD.get(val, 0)
            b2 |= _B2.get(val, 0)
    body = struct.pack("<I", MSG_PAD_DATA)
    body += struct.pack("<BBBB", 0, 2, 2, 2)  # slot 0, connected, full gyro, bluetooth
    body += MAC + struct.pack("<BB", 0x05, 1)  # battery full, active
    body += struct.pack("<I", counter & 0xFFFFFFFF)
    body += struct.pack("<BBBB", b1, b2, ps, touch)
    body += struct.pack("<BBBB", 128, 128, 128, 128)  # sticks centred
    body += bytes(analog)
    body += bytes(12)  # touch 1 + 2
    body += struct.pack("<Q", timestamp_us & 0xFFFFFFFFFFFFFFFF)
    body += struct.pack("<6f", 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)  # accel (1 g down), gyro
    return body


class DSUServer:
    """The pad server: one UDP socket, a receive thread and a send thread (daemons).

    ``start()`` never raises: a port already taken gives ``False`` and
    ``error``. Thread-safe; ``press`` blocks for ``hold + gap``, ``tap`` does not
    (the send thread releases the button).
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = HOST, rate: float = 250.0) -> None:
        self.port = port
        self.host = host
        self.rate = rate
        self.error: str | None = None
        self.sent = 0
        self.uid = int.from_bytes(os.urandom(4), "little")
        self._sock: socket.socket | None = None
        self._held: set[str] = set()
        self._release_at: dict[str, float] = {}
        self._clients: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._running = False
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def start(self, port: int | None = None) -> bool:
        """Bind and start serving (restart on another port). False + ``error`` when the bind fails."""
        if port is not None and port != self.port:
            self.stop()
            self.port = port
        if self._running:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            self.error = f"UDP port {self.port} is already in use ({exc.strerror or exc}): pick another DSU port"
            return False
        sock.settimeout(0.05)
        self._sock = sock
        self.error = None
        self._running = True
        self._wake.clear()
        self._threads = [
            threading.Thread(target=fn, daemon=True, name=f"ebc-dsu-{name}")
            for name, fn in (("rx", self._rx), ("tx", self._tx))
        ]
        for t in self._threads:
            t.start()
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._wake.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(1.0)
        self._threads = []
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        with self._lock:
            self._clients.clear()
            self._held.clear()
            self._release_at.clear()

    # -- state ----------------------------------------------------------------------

    @property
    def clients(self) -> int:
        """Dolphins that asked for our pad data in the last few seconds."""
        now = time.monotonic()
        with self._lock:
            return sum(1 for t in self._clients.values() if now - t < CLIENT_TIMEOUT)

    @property
    def connected(self) -> bool:
        return self._running and self.clients > 0

    def wait_client(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            time.sleep(0.02)
        return self.connected

    # -- buttons --------------------------------------------------------------------

    def set(self, button: str, down: bool) -> None:
        if button not in BUTTONS:
            raise KeyError(f"unknown DSU button {button!r}")
        with self._lock:
            if down:
                self._held.add(button)
            else:
                self._held.discard(button)
            self._release_at.pop(button, None)
        self._wake.set()

    def tap(self, button: str, hold: float = HOLD) -> bool:
        """Press ``button`` now, release it after ``hold`` seconds (non-blocking)."""
        if not self.connected:
            return False
        self.set(button, True)
        with self._lock:
            self._release_at[button] = time.monotonic() + hold
        return True

    def press(self, button: str, hold: float = HOLD, gap: float = GAP) -> bool:
        """Press, hold, release, wait ``gap`` (blocking). False when no Dolphin listens."""
        if not self.connected:
            return False
        self.set(button, True)
        time.sleep(hold)
        self.set(button, False)
        time.sleep(gap)
        return True

    # -- what EBC uses ------------------------------------------------------------------

    def screenshot(self) -> bool:
        return self.press(SCREENSHOT)

    def frame_advance(self) -> bool:
        """+1 frame when paused; pauses WITHOUT advancing when the game runs."""
        return self.press(FRAME_ADVANCE)

    def toggle_pause(self) -> bool:
        return self.press(TOGGLE_PAUSE)

    def save_slot(self) -> bool:
        return self.press(SAVE_SLOT)

    def load_slot(self) -> bool:
        return self.press(LOAD_SLOT)

    # -- protocol threads ---------------------------------------------------------------

    def _pad_packet(self) -> bytes:
        with self._lock:
            held = frozenset(self._held)
            self._counter = (self._counter + 1) & 0xFFFFFFFF
            counter = self._counter
        return packet(self.uid, pad_body(held, counter, int(time.monotonic() * 1e6)))

    def _rx(self) -> None:
        while self._running:
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:  # timeout, or closed by stop()
                continue
            if len(data) < HEADER.size + 4 or data[:4] != b"DSUC":
                continue
            (kind,) = struct.unpack_from("<I", data, HEADER.size)
            try:
                if kind == MSG_VERSION:
                    sock.sendto(packet(self.uid, version_body()), addr)
                elif kind == MSG_PORTS and len(data) >= HEADER.size + 8:
                    (count,) = struct.unpack_from("<i", data, HEADER.size + 4)
                    for slot in data[HEADER.size + 8 : HEADER.size + 8 + max(0, min(count, 4))]:
                        sock.sendto(packet(self.uid, port_info_body(slot)), addr)
                elif kind == MSG_PAD_DATA:
                    with self._lock:
                        self._clients[addr] = time.monotonic()
                    self._wake.set()
            except OSError:
                pass

    def _tx(self) -> None:
        period = 1.0 / self.rate
        while self._running:
            self._wake.wait(period)
            self._wake.clear()
            now = time.monotonic()
            with self._lock:
                for button, at in list(self._release_at.items()):
                    if now >= at:
                        self._held.discard(button)
                        del self._release_at[button]
                targets = [a for a, t in self._clients.items() if now - t < CLIENT_TIMEOUT]
            sock = self._sock
            if not targets or sock is None:
                continue
            pkt = self._pad_packet()
            for addr in targets:
                try:
                    sock.sendto(pkt, addr)
                    self.sent += 1
                except OSError:
                    pass
