"""Forwarding: hosts deliver locally or via their gateway; routers find the cheapest path."""
from __future__ import annotations

import heapq

from ..constants import VIP_HOST
from .base import NetworkBase
from .types import Leg, NetError, fmt


class RoutingMixin(NetworkBase):
    def route(self, src: str, dst: tuple[str, int] | None) -> list[Leg]:
        """Path from a device to an interface (or to the internet if dst is None).

        Each leg is one layer-2 hop between addressed interfaces. A host delivers
        directly on its own network, otherwise via its gateway; routers know every
        network of every router they share a network with (a toy IGP).
        """
        dev = self.devices[src]
        if dev.kind == "s":
            raise NetError(f"{src} is a switch: layer 2 only, it has no address to send from")
        if not dev.up:
            raise NetError(f"{src} is down")
        if dst and dst[0] == src:
            return []
        dst_addr = self.addr_of(dst) if dst else None
        if dev.kind in "rg":
            return self._router_route(src, dst, dst_addr)
        nics = self.addressed(src)
        if not nics:
            raise NetError(f"{src} has no address (try: dhcp {src})")
        if dst:
            for p, ifc in nics:
                if ifc.net == dst_addr[0] and dst in self.segment(src, p):
                    return [((src, p), dst, self.l2path((src, p), dst))]
        problems = []
        for p, ifc in nics:
            if ifc.gw is None:
                continue
            seg = self.segment(src, p)
            if ifc.gw == VIP_HOST:
                owners = self.vip_owners(ifc.net)
                here = [o for o in owners if o in seg]
                if len(owners) > 1:
                    self.notes.append(f"⚠ virtual gateway {fmt((ifc.net, VIP_HOST))} claimed by "
                                      f"{' and '.join(o[0] for o in owners)} (ARP conflict: "
                                      f"{src} happens to reach {here[0][0] if here else 'neither'})")
                elif here:
                    self.notes.append(f"gateway {fmt((ifc.net, VIP_HOST))} is virtual, "
                                      f"owned by {here[0][0]} (active)")
                gw = here[0] if here else None
            else:
                gw = next((e for e in seg if self.addr_of(e) == (ifc.net, ifc.gw)), None)
            if gw is None:
                problems.append(f"gateway {fmt((ifc.net, ifc.gw))} unreachable from {src}:{dev.port_name(p)}")
                continue
            try:
                return [((src, p), gw, self.l2path((src, p), gw))] + self._router_route(gw[0], dst, dst_addr)
            except NetError as e:
                problems.append(str(e))
        if problems:
            raise NetError("; ".join(problems))
        where = f"network {dst_addr[0]}" if dst_addr else "the internet"
        raise NetError(f"{where} isn't local to {src} and {src} has no gateway")

    def _router_route(self, start: str, dst: tuple[str, int] | None,
                      dst_addr: tuple[int, int] | None) -> list[Leg]:
        """Cheapest path in cable hops from a router to dst (or to any gateway).

        Routers are nodes; two routers are adjacent when they share a network.
        Counting cables rather than routers lets a direct fabric link win over
        a detour back through a switched network."""
        best: dict[str, int] = {start: 0}
        prev: dict[str, tuple[str, Leg] | None] = {start: None}
        heap: list[tuple[int, int, str, Leg | None]] = [(0, 0, start, None)]
        tick = 0
        done: set[str] = set()

        def legs_to(u: str) -> list[Leg]:
            out = []
            while prev[u] is not None:
                parent, leg = prev[u]
                out.append(leg)
                u = parent
            return out[::-1]

        while heap:
            cost, _, u, final = heapq.heappop(heap)
            if final is not None:                       # popped the destination itself
                return legs_to(u) + [final]
            if u in done:
                continue
            done.add(u)
            if dst is None and self.devices[u].kind == "g":
                return legs_to(u)
            if dst is not None and dst[0] == u:
                return legs_to(u)
            for p, ifc in self.addressed(u):
                seg = self.segment(u, p)
                if dst is not None and dst in seg and ifc.net == dst_addr[0]:
                    path = self.l2path((u, p), dst)
                    tick += 1
                    heapq.heappush(heap, (cost + len(path) - 1, tick, u, ((u, p), dst, path)))
                for e in seg:
                    a = self.addr_of(e)
                    if not (self.is_router(e[0]) and a and a[0] == ifc.net) or e[0] in done:
                        continue
                    path = self.l2path((u, p), e)
                    c = cost + len(path) - 1
                    if c < best.get(e[0], 10**9):
                        best[e[0]] = c
                        prev[e[0]] = (u, ((u, p), e, path))
                        tick += 1
                        heapq.heappush(heap, (c, tick, e[0], None))
        if dst is None:
            raise NetError(f"no route from {start} to a gateway (internet)")
        raise NetError(f"no route to network {dst_addr[0]} from {start}")
