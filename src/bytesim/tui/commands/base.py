"""What every command mixin may rely on from the app."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...constants import REF
from ...model import NetError, Network

if TYPE_CHECKING:
    from ..canvas import NetCanvas


class CommandHost:
    """Attributes and helpers the app provides to the command mixins."""

    net: Network
    auto: bool
    graph: bool
    animation_speed: float = 1.0  # session playback multiplier, independent of traffic rate
    paused: bool = False
    replaying: int = 0          # > 0 while a script runs (hosts may batch their side effects)
    headless: bool = False      # True for the screenless ScriptRunner

    @property
    def pending_actions(self) -> list[list[str]]:
        return self.__dict__.setdefault("_pending_actions", [])

    def queue_action(self, tokens: list[str]) -> None:
        raise NotImplementedError

    @property
    def canvas(self) -> "NetCanvas":
        raise NotImplementedError

    def log_msg(self, msg: str) -> None:
        raise NotImplementedError

    def changed(self, skip: frozenset[tuple[str, int]] = frozenset()) -> None:
        raise NotImplementedError

    def refresh_sidebar(self) -> None:
        raise NotImplementedError

    def dispatch(self, toks: list[str]) -> None:
        raise NotImplementedError

    @staticmethod
    def _ref(tok: str) -> tuple[str, int | None]:
        """Parse a device token like ``r1`` or ``s2:5`` into (id, port)."""
        m = REF.match(tok)
        if not m:
            raise NetError(f"not a device id: {tok!r} (expected something like r1 or s2:5)")
        return f"{m[1].lower()}{int(m[2])}", int(m[3]) if m[3] else None
