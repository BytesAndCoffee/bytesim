"""Forwarding: hosts deliver locally or via their gateway; routers forward hop by
hop by their routing tables.

Every router keeps a table: its connected networks, the networks it learns from
the others, static routes it was given, and a default route toward the gateways
that lead to the internet. Tables are built by a link-state protocol: each
router knows the router graph (two routers are neighbours when they share a
network; a cable costs one) and runs shortest paths from itself. It converges
instantly: whenever the topology changes, every table is rebuilt before the
next packet. A packet then crosses one router at a time, each looking up the
destination's network in its own table (or falling back to its default route).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ..constants import TTL, VIP_HOST
from .base import NetworkBase
from .layer2 import flow_choice, route_query
from .types import Leg, NetError, fmt

DEFAULT = 0          # the default route's key in a table (network ids start at 1)


@dataclass
class Route:
    kind: str                   # connected, learned, static, drop (static blackhole), default, exit
    cost: int = 0
    hops: list[tuple[tuple[str, int], tuple[str, int]]] = field(default_factory=list)
    #   (egress interface, next hop's interface), one per equal-cost way (ECMP)
    via: tuple[int, int] | None = None   # a static route's next-hop address


class RoutingMixin(NetworkBase):
    _rib: tuple | None = None   # (routing state, router graph, {router: table}); rebuilt when the state moves

    @route_query
    def route(self, src: str, dst: tuple[str, int] | None, port: int | None = None,
              nat: bool = True) -> list[Leg]:
        """Path from a device to an interface (or to the internet if dst is None).

        Each leg is one layer-2 hop between addressed interfaces. A host delivers
        directly on its own network, otherwise via its gateway; routers forward by
        their tables. A ``port`` binds a computer's packet to that interface (like
        ``ping -I``). ``nat=False`` routes as if NAT gateways advertised their LANs:
        the path a packet takes once a mapping (or a punched hole) lets it in.

        Where several next hops are equally cheap, the flow (source and destination)
        picks one at each router: the same flow always takes the same path, different
        flows spread out (ECMP). Hops over a link aggregation pick their member the same way.
        """
        dev = self.devices[src]
        if dev.kind == "s":
            raise NetError(f"{src} is a switch: layer 2 only, it has no address to send from")
        if not dev.up:
            raise NetError(f"{src} is down")
        if port is not None and dev.kind in "rg":
            raise NetError(f"{src} is a router: it picks its own outgoing interface "
                           f"(send from {src}, not {src}:{dev.port_name(port)})")
        if dst and dst[0] == src:
            return []
        dst_addr = self.addr_of(dst) if dst else None
        flow = f"{src}>{dst[0] if dst else 'internet'}"
        if dev.kind in "rg":
            return self._router_route(src, dst, dst_addr, flow, nat)
        nics = self.addressed(src)
        if port is not None:
            nics = [(p, ifc) for p, ifc in nics if p == self.lag_primary(src, port)]
            if not nics:
                raise NetError(f"{src}:{dev.port_name(port)} has no address")
        if not nics:
            raise NetError(f"{src} has no address (try: dhcp {src})")
        if dst:
            on_link = [p for p, ifc in nics if ifc.net == dst_addr[0]]
            for p in on_link:
                if dst in self.segment(src, p):
                    return [((src, p), dst, self.l2path((src, p), dst, flow))]
            if on_link:                     # its own network: it asks by ARP, never its gateway
                raise NetError(f"{fmt(dst_addr)} is on {src}'s own network {dst_addr[0]}, but nothing on "
                               f"{src}:{dev.port_name(on_link[0])}'s link has it (ARP got no reply)")
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
            if gw[0] in self.standby_routers() and not (dst is not None and dst[0] == gw[0]):  # type: ignore[attr-defined]
                problems.append(f"gateway {fmt((ifc.net, ifc.gw))} is {gw[0]}, an active-passive "
                                f"standby: it doesn't forward")
                continue
            try:
                return ([((src, p), gw, self.l2path((src, p), gw, flow))]
                        + self._router_route(gw[0], dst, dst_addr, flow, nat))
            except NetError as e:
                problems.append(str(e))
        if problems:
            raise NetError("; ".join(problems))
        where = f"network {dst_addr[0]}" if dst_addr else "the internet"
        who = src if port is None else f"{src}:{dev.port_name(port)}"
        raise NetError(f"{where} isn't local to {who} and {who} has no gateway")

    def _router_route(self, start: str, dst: tuple[str, int] | None,
                      dst_addr: tuple[int, int] | None, flow: str = "", nat: bool = True) -> list[Leg]:
        """Forward from a router, one table lookup per router, to dst (or out to the
        internet). A routing loop (from static routes) runs until the TTL would expire."""
        legs: list[Leg] = []
        u = start
        while len(legs) <= TTL:
            if dst is not None and dst[0] == u:
                return legs
            table = self.routing_table(u, nat)
            if dst is None:
                route = table.get(DEFAULT)
                if route is None:
                    raise NetError(f"no route from {u} to a gateway (internet)")
                if route.kind == "exit":
                    return legs
            else:
                net = dst_addr[0]
                here = [p for p, ifc in self.addressed(u) if ifc.net == net and self._port_live(u, p)]
                for p in here:
                    if dst in self.segment(u, p):
                        path = self.l2path((u, p), dst, flow)
                        if path is not None:
                            legs.append(((u, p), dst, path))
                            return legs
                if here:
                    raise NetError(f"{fmt(dst_addr)} is on {u}'s network {net}, but nothing on "
                                   f"{u}:{self.devices[u].port_name(here[0])}'s link has it (ARP got no reply)")
                route = table.get(net) or table.get(DEFAULT)
                if route is None or route.kind == "exit":
                    raise NetError(f"no route to network {net} from {u}")
            if route.kind == "drop":
                what = f"network {dst_addr[0]}" if dst is not None and table.get(dst_addr[0]) is route else "it"
                raise NetError(f"{u} drops traffic for {what} (static route: drop)")
            a, e = flow_choice(route.hops, flow, u)
            path = self.l2path(a, e, flow)
            if path is None:
                raise NetError(f"{u}'s next hop {fmt(self.addr_of(e))} is unreachable")  # type: ignore[attr-defined]
            legs.append((a, e, path))
            u = e[0]
        return legs                                     # looping: the TTL runs out on the way

    def _port_live(self, dev_id: str, port: int) -> bool:
        li = self._logical_link(dev_id, port)  # type: ignore[attr-defined]
        return li is not None and self.usable(self.links[li])  # type: ignore[attr-defined]

    # ---- routing tables ------------------------------------------------------
    def _routing_state(self) -> tuple:
        """Everything a table depends on; tables are rebuilt when it changes."""
        return (tuple((l.a, l.pa, l.b, l.pb, l.role, l.up) for l in self.links),
                tuple((d.id, d.up, d.nports) for d in self.devices.values()),
                tuple((k, i.net, i.host) for k, i in self.ifaces.items()),
                frozenset(self.stp_blocked), frozenset(self.storming),
                tuple(sorted(self.clusters.items())), tuple(sorted(self.cluster_modes.items())),
                frozenset(self.lags), frozenset(self.mlag_domains), self.mlag_state_key(),  # type: ignore[attr-defined]
                repr(sorted(self.static_routes.items())), self.internet,
                tuple(self.devices_in(self.internet)) if self.internet in self.groups else (),  # type: ignore[attr-defined]
                frozenset(self.nat_off), frozenset(self.overlays))

    def _tables(self) -> tuple[dict, dict]:
        memo = self._route_memo
        if memo is not None and "rib" in memo:          # one state check per route query
            return memo["rib"]
        state = self._routing_state()
        if self._rib is None or self._rib[0] != state:
            self._rib = (state, self._router_graph(), {})
        if memo is not None:
            memo["rib"] = self._rib[1], self._rib[2]
        return self._rib[1], self._rib[2]

    def routing_table(self, router: str, nat: bool = True) -> dict[int, Route]:
        """A router's table: network id (DEFAULT for the default route) -> Route."""
        graph, tables = self._tables()
        if (router, nat) not in tables:
            with self.remembering():  # type: ignore[attr-defined]
                tables[router, nat] = self._build_table(router, graph, nat)
        return tables[router, nat]

    def _router_graph(self) -> dict[str, list[tuple[int, tuple[str, int], tuple[str, int]]]]:
        """Each live router's neighbours: (cable hops, its interface, theirs). What a
        router advertises past a NAT is limited separately (see advertises())."""
        graph: dict[str, list] = {}
        with self.remembering():  # type: ignore[attr-defined]
            for d in self.sorted_devices():
                if d.kind not in "rg" or not d.up:
                    continue
                out = graph.setdefault(d.id, [])
                for p, ifc in self.addressed(d.id):
                    for e in self.segment(d.id, p):
                        a = self.addr_of(e)
                        if not (self.is_router(e[0]) and a and a[0] == ifc.net):
                            continue
                        path = self.l2path((d.id, p), e)
                        if path is not None:
                            out.append((len(path) - 1, (d.id, p), e))
        return graph

    def _build_table(self, u: str, graph: dict, nat: bool = True) -> dict[int, Route]:
        standby = self.standby_routers()  # type: ignore[attr-defined]
        dist: dict[str, int] = {u: 0}
        first: dict[str, set] = {u: set()}
        entered: dict[str, int | None] = {u: None}      # the port each router is first reached by

        def hidden(v: str, port: int) -> bool:          # behind v's NAT, seen from its WAN side
            return nat and v != u and self.nat_hides(v, entered[v], port)  # type: ignore[attr-defined]

        heap: list[tuple[int, str]] = [(0, u)]
        done: list[str] = []
        while heap:
            cost, v = heapq.heappop(heap)
            if v in done or cost > dist[v]:
                continue
            done.append(v)
            if v != u and v in standby:
                continue                                 # an active-passive standby doesn't forward
            for c, a, e in graph.get(v, []):
                w, nc = e[0], cost + c
                hops = {(a, e)} if v == u else first[v]
                if hidden(v, a[1]):
                    continue
                if nc < dist.get(w, 10**9):
                    dist[w], first[w], entered[w] = nc, set(hops), e[1]
                    heapq.heappush(heap, (nc, w))
                elif nc == dist[w]:
                    first[w] |= hops
        table: dict[int, Route] = {}
        attached: dict[int, list[str]] = {}
        for v in done:
            if v != u and v in standby:
                continue
            for p, ifc in self.addressed(v):
                if not self._port_live(v, p):
                    continue
                if v == u:
                    table.setdefault(ifc.net, Route("connected"))
                elif not hidden(v, p):
                    attached.setdefault(ifc.net, []).append(v)
        for net, vs in attached.items():
            if net in table:
                continue
            best = min(dist[v] for v in vs)
            hops = set().union(*(first[v] for v in vs if dist[v] == best))
            table[net] = Route("learned", best, sorted(hops))
        public = self.public()  # type: ignore[attr-defined]
        exits = [v for v in done if self.devices[v].kind == "g" and (not public or v in public)
                 and (v == u or v not in standby)]
        if u in exits:
            table[DEFAULT] = Route("exit")
        elif exits:
            best = min(dist[v] for v in exits)
            hops = set().union(*(first[v] for v in exits if dist[v] == best))
            table[DEFAULT] = Route("default", best, sorted(hops))
        for net, target in self.static_routes.get(u, {}).items():
            if target == "drop":
                table[net] = Route("drop")
                continue
            hop = self._static_hop(u, tuple(target))
            if hop is not None:                          # a next hop it can't reach isn't installed
                table[net] = Route("static", 0, [hop], tuple(target))
        return table

    def _static_hop(self, u: str, via: tuple[int, int]) -> tuple[tuple[str, int], tuple[str, int]] | None:
        for p, ifc in self.addressed(u):
            if ifc.net == via[0] and self._port_live(u, p):
                for e in self.segment(u, p):
                    if self.addr_of(e) == via:
                        return (u, p), e
        return None
