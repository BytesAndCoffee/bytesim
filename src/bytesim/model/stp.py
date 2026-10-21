"""Switching loops: spanning tree (STP), loop detection, flooding, broadcast storms.

Switches run STP unless it is switched off for them. STP picks a root bridge
in every switched area, keeps the shortest path tree to it, and blocks one end
of every other switch-to-switch cable, so no loop forwards frames. A loop with
no STP switch able to block it stays live: the next broadcast into it circles
forever and multiplies. That is a broadcast storm, and it saturates every
cable it touches until the loop is broken.
"""
from __future__ import annotations

import heapq
from collections import Counter, deque
from dataclasses import dataclass, field

from ..constants import RANK
from .base import NetworkBase
from .layer2 import structure_memo
from .types import Link

DEFAULT_PRIORITY = 32768
DEFAULT_COST = 20000          # 802.1D-2004 path cost of a 1 Gb/s port
MAX_COST = 200_000_000


@dataclass

class Flood:
    """What happened to one broadcast frame, generation by generation."""
    source: tuple[str, int]
    generations: list[dict[tuple[int, str], int]] = field(default_factory=list)  # (link, arriving at) -> copies
    delivered: Counter = field(default_factory=Counter)      # (device, port) -> copies received
    storm: bool = False
    storm_switches: set[str] = field(default_factory=set)
    storm_links: set[int] = field(default_factory=set)
    flaps: dict[str, set[int]] = field(default_factory=dict)  # switch -> ports the source MAC arrived on

    @property
    def totals(self) -> list[int]:
        return [sum(g.values()) for g in self.generations]


def adj_links(adj: dict[str, list[tuple[str, int]]]) -> set[int]:
    return {i for edges in adj.values() for _, i in edges}


class SpanningTreeMixin(NetworkBase):
    # ---- configuration -------------------------------------------------
    def stp_enabled(self, switch: str) -> bool:
        return switch not in self.stp_off

    def port_cost(self, switch: str, port: int) -> int:
        """A port's STP path cost (a bundle costs what its cheapest member does)."""
        ports = self.lag_ports(switch, port)  # type: ignore[attr-defined]
        return min(self.stp_cost.get((switch, p), DEFAULT_COST) for p in ports)

    def bridge_id(self, switch: str) -> tuple[int, int, int]:
        """Lower wins the root election: priority, then kind and number."""
        d = self.devices[switch]
        return self.stp_priority.get(switch, DEFAULT_PRIORITY), RANK[d.kind], d.num

    # ---- the switch-to-switch graph -------------------------------------
    def _carries(self, l: Link, honor_stp: bool) -> bool:
        """Could this cable forward frames, storms aside? (STP blocks optional.)"""
        if not (l.up and not l.control and self.devices[l.a].up and self.devices[l.b].up):
            return False
        if (l.a, l.pa) in self.mlag_suspended or (l.b, l.pb) in self.mlag_suspended:
            return False                             # an MLAG secondary's suspended member port
        return not (honor_stp and ((l.a, l.pa) in self.stp_blocked or (l.b, l.pb) in self.stp_blocked))

    def _trunks(self, honor_stp: bool) -> list[int]:
        """Indices of cables joining two switches that can forward frames."""
        return [i for i, l in enumerate(self.links)
                if self.devices[l.a].kind == "s" and self.devices[l.b].kind == "s"
                and self._carries(l, honor_stp)]

    def _areas(self, trunks: list[int]) -> list[set[str]]:
        """Switches grouped into areas joined by the given trunks."""
        adj: dict[str, set[str]] = {d.id: set() for d in self.devices.values() if d.kind == "s"}
        for i in trunks:
            l = self.links[i]
            adj[l.a].add(l.b)
            adj[l.b].add(l.a)
        out, seen = [], set()
        for s in sorted(adj, key=lambda s: self.bridge_id(s)):
            if s in seen:
                continue
            comp, q = {s}, deque([s])
            while q:
                for v in adj[q.popleft()]:
                    if v not in comp:
                        comp.add(v)
                        q.append(v)
            seen |= comp
            out.append(comp)
        return out

    def loop_links(self, honor_stp: bool = True) -> set[int]:
        """Trunks that lie on a loop (any cable that isn't a bridge in the graph theory sense)."""
        trunks = self._trunks(honor_stp)
        rep: dict[frozenset, int] = {}             # a link aggregation counts as one link
        for i in trunks:
            pair = self.bundle(self.links[i])  # type: ignore[attr-defined]
            if pair is not None:
                rep.setdefault(pair, i)
        adj: dict[str, list[tuple[str, int]]] = {}
        for i in trunks:
            l = self.links[i]
            pair = self.bundle(l)  # type: ignore[attr-defined]
            if pair is not None and rep[pair] != i:
                continue
            adj.setdefault(l.a, []).append((l.b, i))
            adj.setdefault(l.b, []).append((l.a, i))
        disc: dict[str, int] = {}
        low: dict[str, int] = {}
        bridges: set[int] = set()
        timer = [0]

        def dfs(u: str, via: int) -> None:            # Tarjan's bridge finding (multigraph-safe)
            disc[u] = low[u] = timer[0]
            timer[0] += 1
            for v, i in adj.get(u, []):
                if i == via:
                    continue
                if v in disc:
                    low[u] = min(low[u], disc[v])
                else:
                    dfs(v, i)
                    low[u] = min(low[u], low[v])
                    if low[v] > disc[u]:
                        bridges.add(i)

        for s in adj:
            if s not in disc:
                dfs(s, -1)
        loops = adj_links(adj) - bridges

        def stands_for(i: int) -> int:               # a bundle's members share its fate
            pair = self.bundle(self.links[i])  # type: ignore[attr-defined]
            return rep[pair] if pair is not None else i

        return {i for i in trunks if stands_for(i) in loops}

    # ---- spanning tree ---------------------------------------------------
    @structure_memo
    def stp_compute(self) -> tuple[set[tuple[str, int]], dict[str, str], dict[str, int]]:
        """Blocked ports, each switch's root bridge, and each switch's root port.

        Per switched area, the root is the STP switch with the lowest bridge id.
        Switches without STP pass STP frames straight through (as real ones do),
        so they can't block anything but don't hide loops either. Cables with
        no STP end always forward; every other cable is added nearest-the-root
        first, and one that would close a loop is blocked at its end farther
        from the root (or its only STP end). What's left is loop-free except
        for loops made entirely of non-STP switches, which nothing can break.
        """
        trunks = self._trunks(honor_stp=False)
        blocked: set[tuple[str, int]] = set()
        roots: dict[str, str] = {}
        root_ports: dict[str, int] = {}
        for area in self._areas(trunks):
            bridges = [s for s in area if self.stp_enabled(s)]
            if not bridges:
                continue
            root = min(bridges, key=self.bridge_id)
            for s in area:
                roots[s] = root
            here = [i for i in trunks if self.links[i].a in area]
            cost = self._costs_from(root, here)

            def via(i: int) -> tuple[str, int, int]:
                """(far end, its port, the root path cost it would have through cable i)."""
                l = self.links[i]
                (near, _), (far, fp) = sorted([(l.a, l.pa), (l.b, l.pb)],
                                               key=lambda e: (cost[e[0]], self.bridge_id(e[0])))
                return far, fp, cost[near] + self.port_cost(far, fp)

            parent = {s: s for s in area}           # union-find over what forwards

            def find(s: str) -> str:
                while parent[s] != s:
                    parent[s] = parent[parent[s]]
                    s = parent[s]
                return s

            def rank(i: int) -> tuple:
                # Cables in the order the far end would pick them as its root port: root
                # path cost through them, then the designated bridge's id and port.
                l = self.links[i]
                near, _ = sorted([(l.a, l.pa), (l.b, l.pb)], key=lambda e: (cost[e[0]], self.bridge_id(e[0])))
                return (via(i)[2], self.bridge_id(near[0]), near[1])

            # A link aggregation is one logical link: decided once, applied to every member.
            groups: dict[object, list[int]] = {}
            for i in here:
                groups.setdefault(self.bundle(self.links[i]) or i, []).append(i)  # type: ignore[attr-defined]
            reps = {g[0]: g for g in groups.values()}
            forced = [i for i in reps if not (self.stp_enabled(self.links[i].a)
                                              or self.stp_enabled(self.links[i].b))]
            for i in forced + sorted((i for i in reps if i not in forced), key=rank):
                l = self.links[i]
                ra, rb = find(l.a), find(l.b)
                if ra != rb:
                    parent[ra] = rb
                    continue
                if i in forced:
                    continue                         # an unbreakable loop: no STP end
                ends = sorted([(l.a, l.pa), (l.b, l.pb)],
                              key=lambda e: (cost[e[0]], self.bridge_id(e[0])), reverse=True)
                side = next(e for e in ends if self.stp_enabled(e[0]))[0]
                for j in reps[i]:                    # every member, at the same end of the bundle
                    lj = self.links[j]
                    at = side if lj.touches(side) else next(
                        d for d in (lj.a, lj.b) if self.end(d) == self.end(side))  # type: ignore[attr-defined]
                    blocked.add((at, lj.port_of(at)))
            # Root port: the forwarding cable that leads one hop closer to the root.
            fwd = [i for i in here if (self.links[i].a, self.links[i].pa) not in blocked
                   and (self.links[i].b, self.links[i].pb) not in blocked]
            dist = self._costs_from(root, fwd)
            for s in area:
                if s == root or not self.stp_enabled(s) or s not in dist:
                    continue
                best = min(((dist[o] + self.port_cost(s, self.links[i].port_of(s)), self.bridge_id(o),
                             self.links[i].port_of(s))
                            for i in fwd if self.links[i].touches(s)
                            and dist.get(o := self.links[i].other(s)[0], 10**18) < dist[s]), default=None)
                if best:
                    root_ports[s] = best[2]
        return blocked, roots, root_ports

    def _costs_from(self, start: str, trunks: list[int]) -> dict[str, int]:
        """Root path costs: entering a switch costs the port it enters by."""
        dist = {start: 0}
        heap = [(0, start)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for i in trunks:
                l = self.links[i]
                if l.touches(u):
                    v, vp = l.other(u)
                    c = d + self.port_cost(v, vp)
                    if c < dist.get(v, 10**18):
                        dist[v] = c
                        heapq.heappush(heap, (c, v))
        return dist

    def root_cost(self, switch: str) -> int | None:
        """A switch's root path cost over forwarding cables (None outside a tree)."""
        root = self.stp_roots.get(switch)
        if root is None:
            return None
        fwd = [i for i in self._trunks(honor_stp=True) if self.stp_roots.get(self.links[i].a) == root]
        return self._costs_from(root, fwd).get(switch)

    def port_role(self, switch: str, port: int) -> str:
        """root / designated / blocking / edge, or '' (unplugged, or STP off)."""
        li = self.link_index(switch, port)
        if li is None:
            return ""
        if (switch, port) in self.stp_blocked:
            return "blocking"
        if self.devices[self.links[li].other(switch)[0]].kind != "s":
            return "edge"
        if not self.stp_enabled(switch) or switch not in self.stp_roots:
            return ""
        return "root" if self.stp_root_ports.get(switch) == port else "designated"

    def stp_update(self) -> list[str]:
        """Re-run STP and end storms whose loop is gone. Returns notes on what changed."""
        notes = self.mlag_update()  # type: ignore[attr-defined]
        before, roots_before = self.stp_blocked, self.stp_roots
        self.stp_blocked, self.stp_roots, self.stp_root_ports = self.stp_compute()
        for sw, p in sorted(before - self.stp_blocked):
            li = self.link_index(sw, p) if sw in self.devices else None
            if li is not None and self._carries(self.links[li], honor_stp=False):
                notes.append(f"STP {sw}:p{p} blocking → forwarding")
        for sw, p in sorted(self.stp_blocked - before):
            notes.append(f"STP {sw}:p{p} → blocking")
        # A topology change (TCN): every STP switch in the changed area, as it was or
        # is now, flushes its MAC table, since what it learned may now lie the other
        # way. Switches with STP off never hear of it.
        changed = {sw for sw, _ in before ^ self.stp_blocked if sw in self.devices}
        if changed:
            roots = {r for sw in changed for r in (roots_before.get(sw), self.stp_roots.get(sw)) if r}
            self.forget_macs({sw for sw in self.devices  # type: ignore[attr-defined]
                              if self.devices[sw].kind == "s" and self.stp_enabled(sw)
                              and (roots_before.get(sw) in roots or self.stp_roots.get(sw) in roots)})
        if self.storming:
            # A storm lives as long as its area still has a forwarding loop.
            looped = {d for i in self.loop_links(honor_stp=True)
                      for d in (self.links[i].a, self.links[i].b)}
            still: set[str] = set()
            for area in self._areas(self._trunks(honor_stp=True)):
                if area & self.storming and area & looped:
                    still |= area & self.storming
            ended = self.storming - still
            if ended:
                self.storming = still
                for s in ended:
                    self.mac_flaps.pop(s, None)
                notes.append(f"broadcast storm over: {' '.join(sorted(ended, key=self.bridge_id))} "
                             f"(loop broken)")
        return notes

    # ---- flooding ----------------------------------------------------------
    @structure_memo
    def flood(self, src: tuple[str, int], dst_mac: str | None = None) -> Flood:
        """Send one broadcast frame out of an interface and follow every copy.

        Switches copy it out of every forwarding port except the one it came in
        on; anything else just receives it. With ``dst_mac`` it's a unicast frame
        instead: a switch that knows that MAC sends it out of that one port (or drops
        it, if that's where it came in), and only one that doesn't floods it. Copies are counted exactly per
        (cable, direction), so even an exponential storm stays cheap to model.
        A loop-free area empties within as many hops as there are cables; if
        frames are still alive after that, they are circling: a storm.
        """
        out = Flood(source=src)
        li0 = self._logical_link(*src)  # type: ignore[attr-defined]
        if li0 is None:
            return out

        def ok(l: Link) -> bool:
            return (self._carries(l, honor_stp=True)
                    and l.a not in self.storming and l.b not in self.storming)

        if not ok(self.links[li0]):
            return out
        state: Counter = Counter({(li0, self.links[li0].other(src[0])[0]): 1})
        for _ in range(len(self.links) + 2):
            out.generations.append(dict(state))
            nxt: Counter = Counter()
            for (li, dev), n in state.items():
                port = self.links[li].port_of(dev)
                if self.devices[dev].kind != "s":
                    out.delivered[(dev, port)] += n
                    continue
                out.flaps.setdefault(dev, set()).add(port)
                came_by = self.bundle(self.links[li])  # type: ignore[attr-defined]
                if dst_mac is not None and (lj := self.known_link(dev, dst_mac)) is not None:  # type: ignore[attr-defined]
                    if lj != li and (came_by is None or self.bundle(self.links[lj]) != came_by) and ok(self.links[lj]):  # type: ignore[attr-defined]
                        nxt[(lj, self.links[lj].other(dev)[0])] += n
                    continue                         # known: one port, or filtered
                # vPC loop avoidance: what crossed a peer link never leaves on a bundle of that domain
                over_peer = self.end(dev) if self.is_peer_link(self.links[li]) else None  # type: ignore[attr-defined]
                bundles_used: set[frozenset] = set()
                for lj, l in self.adjacent_links(dev):
                    if lj != li and ok(l):
                        pair = self.bundle(l)  # type: ignore[attr-defined]
                        if pair is not None:       # one member per bundle, never back the way it came
                            if pair == came_by or pair in bundles_used or over_peer in pair:
                                continue
                            bundles_used.add(pair)
                        nxt[(lj, l.other(dev)[0])] += n
            if not nxt:
                break
            state = nxt
        else:
            out.storm = True
            # Everything the circling frames touched over the last lap (a loop can't
            # be longer than the number of cables), not just where they are now.
            lap = out.generations[-(len(self.links) + 1):] + [dict(state)]
            out.storm_links = {li for gen in lap for li, _ in gen}
            out.storm_switches = {d for li in out.storm_links for d in (self.links[li].a, self.links[li].b)
                                  if self.devices[d].kind == "s"}
        out.flaps = {s: ps for s, ps in out.flaps.items() if len(ps) > 1}
        return out

    def start_storm(self, flood: Flood) -> None:
        """Mark a storming area: its cables carry nothing but the storm from now on."""
        mac = self.devices[flood.source[0]].mac(flood.source[1])
        area = set()
        for a in self._areas(self._trunks(honor_stp=True)):
            if a & flood.storm_switches:
                area |= a
        self.storming |= area
        for s, ports in flood.flaps.items():
            self.mac_flaps.setdefault(s, {})[mac] = ports

    def learn_flood(self, flood: Flood) -> None:
        """Switches a (loop-free) broadcast passed through learn its sender's MAC."""
        mac = self.devices[flood.source[0]].mac(flood.source[1])
        for gen in flood.generations:
            for li, dev in gen:
                if self.devices[dev].kind == "s":
                    self.learn_mac(dev, mac, self.links[li].port_of(dev))  # type: ignore[attr-defined]

    def storm_near(self, dev_id: str) -> set[str]:
        """Storming switches in the switched area(s) this device is plugged into."""
        seen, q, hit = {dev_id}, deque([dev_id]), set()
        while q:
            u = q.popleft()
            for l in self.links:
                if l.touches(u) and self._carries(l, honor_stp=True):
                    v = l.other(u)[0]
                    if v in seen or self.devices[v].kind != "s":
                        continue
                    seen.add(v)
                    q.append(v)
                    if v in self.storming:
                        hit.add(v)
        return hit
