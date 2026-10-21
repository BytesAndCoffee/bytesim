"""Routing tables: show what each router knows, and give it static routes."""
from __future__ import annotations

from ...model import NetError
from ...model.routing import DEFAULT
from ...model.types import fmt
from .base import CommandHost
from .registry import command


class RoutingCommands(CommandHost):
    @command("routes", "rib")
    def cmd_routes(self, args: list[str], opts: dict[str, str]) -> None:
        """routes: every router's table · routes <router…>: some."""
        net = self.net
        routers = ([self._router(a) for a in args] if args
                   else [d.id for d in net.sorted_devices() if d.kind in "rg"])
        if not routers:
            self.log_msg("[dim]no routers or gateways[/]")
        for r in routers:
            self.log_msg(f"[b]{r}[/] routing table" + ("" if net.devices[r].up else " [red](down)[/]"))
            table = net.routing_table(r) if net.devices[r].up else {}
            if not table:
                self.log_msg("  [dim]empty[/]")
            for key in sorted(table, key=lambda k: (k == DEFAULT, k)):
                self.log_msg("  " + self._route_text(r, key, table[key]))

    def _route_text(self, r: str, key: int, route) -> str:
        net = self.net
        where = "default" if key == DEFAULT else f"network {key}"
        if route.kind == "connected":
            return f"{where:12} connected"
        if route.kind == "exit":
            return f"{where:12} [magenta]exit[/] (this gateway leads to the internet)"
        if route.kind == "drop":
            return f"{where:12} [red]drop[/] (static)"
        hops = ", ".join(f"{fmt(net.addr_of(e))} {e[0]} via {a[0]}:{net.devices[a[0]].port_name(a[1])}"
                         for a, e in route.hops)
        tag = "static" if route.kind == "static" else f"cost {route.cost}" + (" · ECMP" if len(route.hops) > 1 else "")
        return f"{where:12} → {hops}  [dim]({tag})[/]"

    @command("route")
    def cmd_route(self, args: list[str], opts: dict[str, str]) -> None:
        """route <router> <network|default> via <(net,host)> | drop | auto."""
        net = self.net
        usage = "usage: route <router> <network|default> via <(net,host)>  ·  … drop  ·  … auto (back to learned)"
        if len(args) < 3:
            raise NetError(usage)
        r = self._router(args[0])
        word = args[1].lower()
        if word == "default":
            key = DEFAULT
        elif word.isdigit() and int(word) >= 1:
            key = int(word)
        else:
            raise NetError(usage)
        what = args[2].lower()
        routes = net.static_routes.setdefault(r, {})
        if what == "via" and len(args) == 4:
            addr = self._parse_addr(args[3])
            if addr is None:
                raise NetError(usage)
            routes[key] = addr
        elif what == "drop" and len(args) == 3:
            routes[key] = "drop"
        elif what in ("auto", "learned", "remove") and len(args) == 3:
            if routes.pop(key, None) is None:
                raise NetError(f"{r} has no static route for {word}")
        else:
            raise NetError(usage)
        if not routes:
            del net.static_routes[r]
        target = routes.get(key)
        where = "the default route" if key == DEFAULT else f"network {key}"
        if target is None:
            self.log_msg(f"{r}: {where} is learned again")
        elif target == "drop":
            self.log_msg(f"{r}: drops traffic for {where} (static)")
        else:
            self.log_msg(f"{r}: {where} via {fmt(target)} (static)")
            if net._static_hop(r, target) is None:
                self.log_msg(f"  [yellow]{fmt(target)} isn't on a live network of {r}: the route stays "
                             f"uninstalled until it is[/]")
        self.changed()

    def _router(self, tok: str) -> str:
        did = self.net.get(self._ref(tok)[0]).id
        if not self.net.is_router(did):
            raise NetError(f"{did} isn't a router or gateway")
        return did
