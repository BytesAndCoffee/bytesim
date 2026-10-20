"""Multi-level undo and redo.

Every command that changes the network is one step: a whole ``run`` script,
``clear`` and ``load`` included. A burst of moves (arrow keys, or one drag) is
one step too. Read-only commands don't create steps, and neither does traffic
that only teaches switches MAC addresses. A storm it sets off does count.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass

from rich.markup import escape

from ...model import NetError, Network
from .base import CommandHost
from .registry import command

UNDO_LIMIT = 100
MOVE_BURST = 2.0          # seconds: moves closer together than this are one step
NOT_RECORDED = {"undo", "redo", "history", "tee", "trace", "detail"}


@dataclass
class Step:
    label: str
    net: Network
    auto: bool
    selection: tuple[str | None, str | None]


def fingerprint(net: Network) -> str:
    """What counts as a change: everything saved, plus storms (not MAC tables)."""
    return net.to_json() + "|" + ",".join(sorted(net.storming))


class HistoryCommands(CommandHost):
    @property
    def undo_stack(self) -> list[Step]:
        return self.__dict__.setdefault("_undo", [])

    @property
    def redo_stack(self) -> list[Step]:
        return self.__dict__.setdefault("_redo", [])

    def snapshot(self, label: str = "") -> Step:
        c = self.canvas
        return Step(label, copy.deepcopy(self.net), self.auto, (c.selected, c.selected_group))

    def record(self, step: Step) -> None:
        """Push a 'before' state; a new change makes the redo stack meaningless."""
        self.undo_stack.append(step)
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()
        self.__dict__["_last_move"] = None

    def recorded(self, label: str, action) -> None:
        """Run an action, keeping one undo step if it changed anything."""
        words = label.split()
        if not words or words[0].lower() in NOT_RECORDED:
            action()
            return
        before, mark = self.snapshot(label), fingerprint(self.net)
        try:
            action()
        finally:
            if fingerprint(self.net) != mark:
                self.record(before)

    def before_move(self, what: str) -> None:
        """Called just before devices move: one step per burst of moves of the same thing."""
        last = self.__dict__.get("_last_move")
        now = time.monotonic()
        if last and last[0] == what and now - last[1] < MOVE_BURST:
            self.__dict__["_last_move"] = (what, now)
            return
        self.record(self.snapshot(f"move {what}"))
        self.__dict__["_last_move"] = (what, now)

    def _restore(self, step: Step) -> None:
        self.net = step.net
        self.pending_actions.clear()
        self.auto = step.auto
        c = self.canvas
        c.reset()
        sel, group = step.selection
        if sel in self.net.devices:
            c.selected = sel
        elif group in self.net.groups:
            c.selected_group = group
        self.__dict__["_last_move"] = None
        self.changed()

    def _travel(self, args: list[str], back: bool) -> None:
        word = "undo" if back else "redo"
        try:
            n = int(args[0]) if args else 1
        except ValueError:
            raise NetError(f"usage: {word} [steps]") from None
        source, target = (self.undo_stack, self.redo_stack) if back else (self.redo_stack, self.undo_stack)
        if not source:
            raise NetError(f"nothing to {word}")
        labels = []
        for _ in range(max(1, n)):
            if not source:
                break
            step = source.pop()
            target.append(self.snapshot(step.label))
            labels.append(step.label)
            self._restore(step)
        arrow = "↶ undid" if back else "↷ redid"
        self.log_msg(f"[cyan]{arrow}[/] " + " · ".join(escape(x) for x in labels)
                     + f"  [dim]({len(self.undo_stack)} to undo, {len(self.redo_stack)} to redo)[/]")

    @command("undo")
    def cmd_undo(self, args: list[str], opts: dict[str, str]) -> None:
        self._travel(args, back=True)

    @command("redo")
    def cmd_redo(self, args: list[str], opts: dict[str, str]) -> None:
        self._travel(args, back=False)

    @command("history")
    def cmd_history(self, args: list[str], opts: dict[str, str]) -> None:
        if not self.undo_stack and not self.redo_stack:
            self.log_msg("[dim]nothing to undo yet[/]")
            return
        for i, step in enumerate(reversed(self.undo_stack[-15:]), 1):
            self.log_msg(f"  {i:>2}  {escape(step.label)}" + ("   [dim]← undo goes back past this[/]" if i == 1 else ""))
        if len(self.undo_stack) > 15:
            self.log_msg(f"  [dim]… {len(self.undo_stack) - 15} older[/]")
        if self.redo_stack:
            self.log_msg(f"  [dim]{len(self.redo_stack)} undone step(s) can be redone: "
                         f"{escape(self.redo_stack[-1].label)} next[/]")
