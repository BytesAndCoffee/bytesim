"""Renumbering devices by BFS or DFS, once or automatically after every change."""
from __future__ import annotations

from ...model import Device, NetError
from ...model.naming import ORDERS
from .base import CommandHost
from .registry import command


class NamingCommands(CommandHost):
    # (order, root device or None) while auto-renumbering is on. The root is held
    # as a Device so it survives being renamed itself.
    naming: tuple[str, Device | None] | None = None

    @command("renumber", "rename")
    def cmd_renumber(self, args: list[str], opts: dict[str, str]) -> None:
        usage = "usage: renumber bfs|dfs [root]  ·  renumber auto bfs|dfs [root]  ·  renumber off"
        if not args:
            self.log_msg(f"auto-renumber: [b]{self.naming_text()}[/]  ({usage[7:]})")
            return
        words = [a.lower() for a in args]
        if words == ["off"]:
            self.naming = None
            self.log_msg("auto-renumber off: ids stay as they are")
            self.refresh_sidebar()
            return
        auto = words[0] == "auto"
        if auto:
            words, args = words[1:], args[1:]
        if not words or words[0] not in ORDERS or len(words) > 2:
            raise NetError(usage)
        order = words[0]
        root = self.net.get(self._ref(args[1])[0]) if len(args) == 2 else None
        if auto:
            self.naming = (order, root)
            self.log_msg(f"auto-renumber on: [b]{self.naming_text()}[/] after every change")
            self.changed()          # applies it now
        else:
            self._apply_renumber(order, root, announce_none=True)
            self.changed()

    def naming_text(self) -> str:
        if self.naming is None:
            return "off"
        order, root = self.naming
        alive = root is not None and root.id in self.net.devices and self.net.devices[root.id] is root
        return f"{order.upper()} from {root.id if alive else self.net.default_root() or '—'}"

    def auto_renumber(self) -> None:
        """Called from changed(): keep ids in traversal order while the mode is on."""
        if self.naming is not None:
            order, root = self.naming
            if root is not None and self.net.devices.get(root.id) is not root:
                root = None          # the root was deleted: fall back to the default
            self._apply_renumber(order, root, announce_none=False)

    def _apply_renumber(self, order: str, root: Device | None, announce_none: bool) -> None:
        c = self.canvas
        mapping = self.net.renumber(order, root.id if root else None)
        if c.selected in mapping:
            c.selected = mapping[c.selected]
        if mapping:
            pairs = sorted(mapping.items(), key=lambda kv: (kv[1][0], int(kv[1][1:])))
            shown = " ".join(f"{old}→{new}" for old, new in pairs[:12])
            more = f" (+{len(pairs) - 12} more)" if len(pairs) > 12 else ""
            self.log_msg(f"[cyan]renumbered ({order.upper()}):[/] {shown}{more}")
        elif announce_none:
            self.log_msg(f"already in {order.upper()} order: nothing to rename")
