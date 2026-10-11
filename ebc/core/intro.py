"""Skip the stage intro camera (docs/research/intro-camera.md). No ``bpy``.

During the intro (match loaded, frame counter 0, about 3.5 s) the camera is written every frame by
the stage task ``stOperatorAppearanceFighter`` (sora_melee), which also clears the match camera's
update bit and puts it back at the end. Two routes, both neutral for the game (fighter positions
and the RNG seed identical to the bit, intro-camera.md 2):

* PATCH (DevServer, recommended): three code words of sora_melee, written with the DevServer's
  ``write`` (it invalidates the JIT cache; DME cannot). P-run and P-end: the operator no longer
  writes the camera; P-bit, only while EBC holds the native freeze: the end of the intro does not
  hand the camera back. The original words are checked before any write, and put back before a
  savestate (the patch would travel in it), when the freeze is released, when the option is
  turned off and on disconnect; they are checked again after a state load and on a new match.
  EBC not driving: the match camera's update bit is set on every tick of the intro, so the game's
  own match camera runs from the first frame.
* DATA (DME only, a stock Dolphin): while EBC drives (native freeze, sync sending), every point the
  operator builds its camera from is EBC's camera on every tick of the intro, and bit 0x80 of
  CameraController+0x44 is cleared until the GO (no frame of the match camera at the end of the
  intro). Limits: the operator forces the roll to 0 during the intro; it is found by a memory search
  for its vtable once per match; EBC not driving: nothing is done (no measured data recipe).

Steady state (option off, or the fight running): no memory access at all.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from .addresses import is_valid_pointer
from .camera import to_brawl
from .overrides import set_bit, write_bit

if TYPE_CHECKING:
    from .camera import CameraPose
    from .game import Game


class Route(StrEnum):
    OFF = "OFF"
    PATCH = "PATCH"  # DevServer code patch
    DATA = "DATA"  # DME data only


ROUTE_OFF = Route.OFF
ROUTE_PATCH = Route.PATCH
ROUTE_DATA = Route.DATA
LABELS = {
    ROUTE_OFF: "off",
    ROUTE_PATCH: "DevServer code patch (exact, roll kept)",
    ROUTE_DATA: "data only (DME): roll 0 during the intro, only while EBC drives the camera",
}
PATCH_NAMES = ("run", "end", "aibit")
MAX_MODULES = 64
SEARCH_CHUNK = 0x10000

CodeWriter = Callable[[int, int], bool]  # (address, 32-bit word) -> written


class IntroSkip:
    def __init__(self, game: Game) -> None:
        self.game = game
        self.enabled = False
        self.route = ROUTE_OFF
        self.error: str | None = None
        self.code_writer: CodeWriter | None = None  # DevServer ``write`` (set by the runtime)
        self.patch_available: Callable[[], bool] = lambda: False
        self.text: int | None = None  # sora_melee .text
        self.patched: frozenset[str] = frozenset()  # words found patched at the last check
        self.op: int | None = None  # stOperatorAppearanceFighter of this match (data route)
        self._op_searched = False  # searched in this match (found or not): never twice
        self._cc_cleared: int | None = None  # CameraController flags address we cleared bit 0x80 at
        self._want_aibit = False

    # -- state ------------------------------------------------------------------------

    def forget(self) -> None:
        """A new connection: nothing known about the game any more (nothing is written)."""
        self.text = None
        self.patched = frozenset()
        self.op = None
        self._op_searched = False
        self._cc_cleared = None
        self.error = None

    @property
    def label(self) -> str:
        if not self.enabled:
            return LABELS[ROUTE_OFF]
        text = LABELS[self.route]
        if self.route == ROUTE_PATCH and self.patched:
            text += f" - patched: {', '.join(sorted(self.patched))}"
        return text

    def set_enabled(self, enabled: bool) -> bool:
        self.enabled = bool(enabled)
        if not self.enabled:
            ok = self.restore()
            self.route = ROUTE_OFF
            self.error = None
            return ok
        self.choose_route()
        if not self.game.connected:
            return False  # the route is chosen again on Connect (the option is pushed then)
        return self.check()

    def choose_route(self) -> str:
        """PATCH when the DevServer can write code, else DATA. A patch in place stays on PATCH."""
        old = self.route
        can_patch = self.code_writer is not None and bool(self.patch_available())
        self.route = ROUTE_PATCH if can_patch or (old == ROUTE_PATCH and self.patched) else ROUTE_DATA
        return self.route

    # -- patch route --------------------------------------------------------------------

    def locate_text(self) -> int | None:
        """sora_melee .text from the OS module list (id 27, section 1)."""
        game, p = self.game, self.game.p
        module = game.deref(p.MODULE_LIST.addr)
        for _ in range(MAX_MODULES):
            if module is None:
                return None
            head = game.read_bytes(module, 0x14, fresh=True)
            if head is None:
                return None
            mid, nxt, _prev, nsec, sections = struct.unpack(">IIIII", head)
            if mid == p.SORA_MELEE_ID:
                if nsec < 2 or not is_valid_pointer(sections):
                    return None
                info = game.read_bytes(sections + 8, 8, fresh=True)
                if info is None:
                    return None
                text = struct.unpack(">I", info[:4])[0] & ~1
                return text if is_valid_pointer(text) else None
            module = nxt if is_valid_pointer(nxt) else None
        return None

    def _patches(self) -> dict[str, tuple[int, int, int]]:
        p = self.game.p
        assert self.text is not None
        return {
            name: (self.text + off, orig, new)
            for name, (off, orig, new) in zip(
                PATCH_NAMES, (p.INTRO_PATCH_RUN, p.INTRO_PATCH_END, p.INTRO_PATCH_AIBIT), strict=True
            )
        }

    def read_words(self) -> dict[str, int] | None:
        """The three code words, in one read (they lie within 0xB0 bytes)."""
        if self.text is None:
            self.text = self.locate_text()
            if self.text is None:
                return None
        patches = self._patches()
        lo = min(a for a, _, _ in patches.values())
        hi = max(a for a, _, _ in patches.values()) + 4
        data = self.game.read_bytes(lo, hi - lo, fresh=True)
        if data is None:
            return None
        return {name: struct.unpack_from(">I", data, a - lo)[0] for name, (a, _, _) in patches.items()}

    def desired(self) -> frozenset[str]:
        if not self.enabled or self.route != ROUTE_PATCH:
            return frozenset()
        want = {"run", "end"}
        if self.game.native_freeze:
            want.add("aibit")  # EBC drives: the end of the intro must not hand the camera back
        return frozenset(want)

    def sync_patch(self, want: frozenset[str] | None = None) -> bool:
        """Make the code words what ``want`` says (default: ``desired()``), original words checked."""
        if want is None:
            want = self.desired()
        self._want_aibit = "aibit" in want
        if not self.game.connected:
            return False
        words = self.read_words()
        if words is None:
            if want:
                self.error = "sora_melee not found in the module list (not in a match yet?)"
            return not want
        patched = self._patched_words(words)
        if patched is None:
            return False
        # put back first (P-bit before P-run / P-end), then patch
        order = [n for n in ("aibit", "end", "run") if n in patched and n not in want]
        order += [n for n in ("run", "end", "aibit") if n in want and n not in patched]
        if order and self.code_writer is None:
            self.error = "the DevServer is needed to write code"
            return False
        ok = self._write_words(order, want, patched)
        if order:  # read back what the DevServer wrote
            words = self.read_words()
            if words is not None:
                patched = {n for n, (_a, _o, new) in self._patches().items() if words[n] == new}
                ok = ok and patched == set(want)
        self.patched = frozenset(patched)
        self.error = None if ok else "the DevServer did not write the patch"
        return ok

    def _patched_words(self, words: dict[str, int]) -> set[str] | None:
        """The words found patched; None (and the error set) when one is neither ours nor the original."""
        patched = set()
        for name, (addr, orig, new) in self._patches().items():
            if words[name] == new:
                patched.add(name)
            elif words[name] != orig:
                self.patched = frozenset(patched)
                self.error = f"unexpected code at 0x{addr:08X}: 0x{words[name]:08X} (not this game build?)"
                return None
        return patched

    def _write_words(self, order: list[str], want: frozenset[str], patched: set[str]) -> bool:
        """Write the words of ``order`` through the DevServer (patch or original); ``patched`` follows."""
        ok = True
        patches = self._patches()
        for name in order:
            addr, orig, new = patches[name]
            word = new if name in want else orig
            if self.code_writer is not None and self.code_writer(addr, word):
                (patched.add if name in want else patched.discard)(name)
            else:
                ok = False
        return ok

    def restore(self) -> bool:
        """Every patched word back to the original (savestate, release, option off, disconnect)."""
        ok = True
        if self.patched or self._want_aibit or (self.enabled and self.route == ROUTE_PATCH):
            ok = self.sync_patch(frozenset())
        return self._release_cc() and ok

    def check(self) -> bool:
        """Connect, state load, new match: the words as they should be (one read when they are)."""
        if not self.enabled:
            return True
        if self.route == ROUTE_PATCH:
            return self.sync_patch()
        return True

    def before_save(self) -> bool:
        """A savestate keeps the RAM: never one with the patch (nor the processCamera bit) in it."""
        return self.restore()

    def after_save(self) -> bool:
        return self.check()

    def after_load(self) -> bool:
        self.op = None
        self._op_searched = False
        self._cc_cleared = None  # the state brought its own CameraController flags back
        return self.check()

    def on_freeze_change(self) -> bool:
        """The native freeze was taken or released: P-bit follows it (released at once)."""
        if self.enabled and self.route == ROUTE_PATCH and ("aibit" in self.desired()) != self._want_aibit:
            return self.sync_patch()
        return True

    def on_new_match(self) -> None:
        """Called by ``Game.poll`` on a new match (outside the tick): the operator is new."""
        self.op = None
        self._op_searched = False
        self._cc_cleared = None  # a new CameraController state
        if self.enabled and self.route == ROUTE_DATA:
            self.op = self.find_operator()
            self._op_searched = True

    # -- data route ------------------------------------------------------------------------

    def find_operator(self) -> int | None:
        """The intro operator by its vtable (+0x3C), heap window first, then MEM1 (direct reads)."""
        game, p = self.game, self.game.p
        needle = struct.pack(">I", p.INTRO_OP_VTABLE)
        for start, size in p.INTRO_OP_SEARCH:
            for addr in game.scan(start, size, needle, SEARCH_CHUNK):
                obj = addr - p.INTRO_OP_VTABLE_AT
                if obj % 4:
                    continue
                state = game.read_bytes(obj + p.INTRO_OP_STATE.offset, 1, fresh=True)
                if state is not None and state[0] <= p.INTRO_OP_DONE:
                    return obj
        self.error = "intro operator not found"
        return None

    def _operator_alive(self, op: int) -> bool:
        p = self.game.p
        return self.game.read_u32(op + p.INTRO_OP_VTABLE_AT) == p.INTRO_OP_VTABLE

    def _release_cc(self) -> bool:
        """processCamera bit back on (it is always set in a match)."""
        addr = self._cc_cleared
        if addr is None:
            return True
        self._cc_cleared = None
        return set_bit(self.game, addr, self.game.p.CC_PROCESS_BIT, True)

    def _clear_cc(self) -> bool:
        p = self.game.p
        cc = self.game.deref(p.CAMERA_CONTROLLER.addr)
        if cc is None:
            return False
        addr = cc + p.CC_FLAGS.offset
        current = self.game.read_bytes(addr, 1)  # tick snapshot: already clear = nothing to do
        if current is not None and self._cc_cleared == addr and not current[0] & p.CC_PROCESS_BIT:
            return True
        ok, found = write_bit(self.game, addr, p.CC_PROCESS_BIT, False)
        if found is not None:
            self._cc_cleared = addr
        return ok

    def _write_sources(self, op: int, eye, target, fov: float) -> bool:
        game, p = self.game, self.game.p
        t = struct.pack(">3f", *target)
        e = struct.pack(">3f", *eye)
        ok = True
        for off in p.INTRO_OP_TARGETS:
            ok = game.write_bytes(op + off, t, skip_same=True) and ok
        for off in p.INTRO_OP_EYES:
            ok = game.write_bytes(op + off, e, skip_same=True) and ok
        offset = struct.pack(">3f", *(eye[i] - target[i] for i in range(3)))
        ok = game.write_bytes(op + p.INTRO_OP_EYE_OFFSET, offset, skip_same=True) and ok
        return game.write_bytes(op + p.INTRO_OP_FOV, struct.pack(">f", fov), skip_same=True) and ok

    # -- per tick ------------------------------------------------------------------------------

    def tick(self, frame: int | None, in_match: bool, pose: CameraPose | None) -> bool:
        """In the sync tick's memory phase, before the camera is written. ``pose``: what EBC sends
        this tick (None when it does not send). No I/O when off or once the fight runs."""
        if not self.enabled:
            return True
        intro = in_match and frame == 0
        if self.route == ROUTE_PATCH:
            return self._tick_patch(intro, pose)
        return self._tick_data(intro, pose)

    def _tick_patch(self, intro: bool, pose: CameraPose | None) -> bool:
        """P-bit follows the freeze; EBC not driving: the game's match camera from the first frame."""
        ok = True
        if ("aibit" in self.desired()) != self._want_aibit:
            ok = self.sync_patch()
        if intro and self.patched and not (pose is not None and self.game.native_freeze):
            ok = self.game.freeze.run_match_camera() and ok
        return ok

    def _tick_data(self, intro: bool, pose: CameraPose | None) -> bool:
        """EBC drives during the intro: the operator's camera sources are ours, processCamera off."""
        if not intro or pose is None or not self.game.native_freeze:
            return self._release_cc()  # nothing to do (and the bit back at the GO / hand back)
        game, p = self.game, self.game.p
        if self.op is None and not self._op_searched:  # enabled or connected during the intro
            self.op = self.find_operator()  # straight from the backend, never via the tick snapshot
            self._op_searched = True
        op = self.op
        if op is None or not self._operator_alive(op):
            return False
        state = game.read_bytes(op + p.INTRO_OP_STATE.offset, 1)
        ok = self._clear_cc()
        if state is not None and state[0] != p.INTRO_OP_DONE:
            fov = pose.fov if pose.fov is not None else game.camera.stage_default_fov()
            if fov is None:
                fov = game.read_value(p.CAM_FOV)
            if fov is not None:
                ok = self._write_sources(op, to_brawl(pose.eye), to_brawl(pose.target), float(fov)) and ok
        return ok
