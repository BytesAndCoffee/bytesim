"""Forwarding: hosts deliver locally or via their gateway; routers find the cheapest path."""
from __future__ import annotations

import heapq

from ..constants import VIP_HOST
from .base import NetworkBase
from .layer2 import flow_pick, route_query
from .types import Leg, NetError, fmt


class RoutingMixin(NetworkBase):
    @route_query
    def route(self, src: str, dst: tuple[str, int] | None) -> list[Leg]:
        """Path from a device to an interface (or to the internet if dst is None).

        Each leg is one layer-2 hop between addressed interfaces. A host delivers
        directly on its own network, otherwise via its gateway; routers know every
        network of every router they share a network with (a toy IGP).

        Where several paths are equally short, the flow (source and destination)
        picks one: the same flow always takes the same path, different flows spread
        out (ECMP). Hops over a link aggregation pick their member the same way.
        """
        dev = self.devices[src]
        if dev.kind == "s":
            raise NetError(f"{src} is a switch: layer 2 only, it has no address to send from")
        if not dev.up:
            raise NetError(f"{src} is down")
        if dst and dst[0] == src:
            return []
        dst_addr = self.addr_of(dst) if dst else None
        flow = f"{src}>{dst[0] if dst else 'internet'}"
        if dev.kind in "rg":
            return self._router_route(src, dst, dst_addr, flow)
        nics = self.addressed(src)
        if not nics:
            raise NetError(f"{src} has no address (try: dhcp {src})")
        if dst:
            for p, ifc in nics:
                if ifc.net == dst_addr[0] and dst in self.segment(src, p):
                    return [((src, p), dst, self.l2path((src, p), dst, flow))]
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
                return ([((src, p), gw, self.l2path((src, p), gw, flow))]
                        + self._router_route(gw[0], dst, dst_addr, flow))
            except NetError as e:
                problems.append(str(e))
        if problems:
            raise NetError("; ".join(problems))
        where = f"network {dst_addr[0]}" if dst_addr else "the internet"
        raise NetError(f"{where} isn't local to {src} and {src} has no gateway")

    def _router_route(self, start: str, dst: tuple[str, int] | None,
                      dst_addr: tuple[int, int] | None, flow: str = "") -> list[Leg]:
        """Cheapest path in cable hops from a router to dst (or to any gateway).

        Routers are nodes; two routers are adjacent when they share a network.
        Counting cables rather than routers lets a direct fabric link win over a
        detour back through a switched network. Every equally cheap way into a
        router is kept, and the flow chooses among them (ECMP)."""
        standby = self.standby_routers()  # type: ignore[attr-defined]
        dist: dict[str, int] = {start: 0}
        ways_in: dict[str, list[tuple[str, Leg]]] = {start: []}
        heap: list[tuple[int, str]] = [(0, start)]
        done: list[str] = []

        while heap:
            cost, u = heapq.heappop(heap)
            if u in done or cost > dist[u]:
                continue
            done.append(u)
            for p, ifc in self.addressed(u):
                for e in self.segment(u, p):
                    a = self.addr_of(e)
                    if not (self.is_router(e[0]) and a and a[0] == ifc.net) or e[0] in done:
                        continue
                    if e[0] in standby and not (dst is not None and dst[0] == e[0]):
                        continue                   # an active-passive standby doesn't forward
                    path = self.l2path((u, p), e, flow)
                    if path is None:
                        continue
                    c = cost + len(path) - 1
                    if c < dist.get(e[0], 10**9):
                        dist[e[0]] = c
                        ways_in[e[0]] = [(u, ((u, p), e, path))]
                        heapq.heappush(heap, (c, e[0]))
                    elif c == dist[e[0]]:
                        ways_in[e[0]].append((u, ((u, p), e, path)))

        # Where the packet can leave the router graph, and how much that costs.
        # With a declared internet, "the internet" means one of its gateways.
        public = self.public() if dst is None else set()  # type: ignore[attr-defined]
        ends: list[tuple[int, str, Leg | None]] = []
        for u in done:
            if dst is None:
                if self.devices[u].kind == "g" and (not public or u in public):
                    ends.append((dist[u], u, None))
                continue
            if dst[0] == u:
                ends.append((dist[u], u, None))
            for p, ifc in self.addressed(u):
                if ifc.net == dst_addr[0] and dst in self.segment(u, p):
                    path = self.l2path((u, p), dst, flow)
                    if path is not None:
                        ends.append((dist[u] + len(path) - 1, u, ((u, p), dst, path)))
        if not ends:
            if dst is None:
                raise NetError(f"no route from {start} to a gateway (internet)")
            raise NetError(f"no route to network {dst_addr[0]} from {start}")

        def order(leg: Leg | None) -> tuple:
            return (leg[0], leg[1]) if leg else ()

        cheapest = min(c for c, _, _ in ends)
        ties = sorted((x for x in ends if x[0] == cheapest), key=lambda x: (x[1], order(x[2])))
        _, u, final = flow_pick(ties, flow, "exit")
        legs: list[Leg] = [final] if final else []
        while u != start:                              # walk back, choosing among equal ways in
            parent, leg = flow_pick(sorted(ways_in[u], key=lambda w: (w[0], order(w[1]))), flow, u)
            legs.append(leg)
            u = parent
        return legs[::-1]
