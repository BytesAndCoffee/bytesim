"""The command line, with history on up/down."""
from __future__ import annotations

from textual.binding import Binding
from textual.widgets import Input

class CommandInput(Input):
    BINDINGS = [
        Binding("escape", "app.focus_canvas", "Canvas", show=False),
        Binding("up", "history(-1)", show=False),
        Binding("down", "history(1)", show=False),
    ]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.history: list[str] = []
        self.hpos = 0

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self.hpos = len(self.history)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        self.hpos = max(0, min(len(self.history), self.hpos + step))
        self.value = self.history[self.hpos] if self.hpos < len(self.history) else ""
        self.cursor_position = len(self.value)
