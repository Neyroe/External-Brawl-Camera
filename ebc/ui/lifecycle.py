"""Session shutdown shared by Disconnect and the add-on's ``unregister``.

One sequence, in this order: the recorder first (its worker reads memory and, frame by frame on
the DSU pad, resumes Dolphin when it ends: the pad must still run), then the timers (sync, stage
reload, screenshot), then the game values put back and the emulator released, then the
DevServer client and the DSU pad. It lives in ``ui`` rather than ``runtime``: it stops UI timers,
and ``runtime`` never imports ``ui``.
"""

from .. import runtime
from . import record, screenshot, stages, sync


def shutdown() -> bool:
    """Stop everything EBC runs and release Dolphin. False when some game value could not be restored."""
    record.stop()
    sync.stop()
    stages.cancel_reload()
    screenshot.stop()
    ok = runtime.get_game().disconnect()
    runtime.devserver.close()
    runtime.dsu.stop()
    return ok
