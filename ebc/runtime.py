"""Process-wide runtime state shared by the UI modules.

Holds the single ``Game`` (memory backend) and ``DevServerClient``. Tests and
the headless smoke test swap the backend with ``set_backend(FakeBackend())``.
"""

from __future__ import annotations

from .core.backend import DMEBackend, MemoryBackend
from .core.devserver import DevServerClient
from .core.game import Game

game = Game(DMEBackend())
devserver = DevServerClient()


def set_backend(backend: MemoryBackend) -> Game:
    """Replace the memory backend (keeps a fresh Game, returns it)."""
    global game
    game = Game(backend)
    return game


def get_game() -> Game:
    return game
