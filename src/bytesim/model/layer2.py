"""Layer 2: which cables carry data, broadcast domains, frame paths, MAC learning."""
from __future__ import annotations

import zlib
from collections import deque

from .base import NetworkBase
from .types import Link


def flow_pick(options: list, flow: str, salt: object = "") -> object:
    """Choose one of several equal options for a flow: stable across runs, the same
    every time for the same flow, spread across different flows (CRC32, not hash())."""
    return options[zlib.crc32(f"{flow}|{salt}".encode()) % len(options)]


class Layer2Mixin(NetworkBase):
    def bundle(self, l: Link) -> frozenset[str] | None:
        """The link aggregation a cable belongs to (its device pair), if any."""
        pair = frozenset((l.a, l.b))
        return pair if pair in self.lags and not l.control else None

    def members(self, pair: frozenset[str]) -> list[int]:
        """Indices of a bundle's cables that can carry data now."""
        return [i for i, l in enumerate(self.links) if self.bundle(l) == pair and self.usable(l)]

    def lag_problem(self, a: str, b: str) -> str | None:
        """Why two devices can't be aggregated, or None if they can."""
        da, db = self.devices[a], self.devices[b]
        if a == b:
            return "a link aggregation joins two different devices"
        if "s" not in (da.kind, db.kind) or not {da.kind, db.kind} <= {"s", "r", "g"}:
            return ("a link aggregation needs a switch at one end, and a switch, router or "
                    "gateway at the other (between routers, parallel cables are routed links "
                    "and ECMP already spreads flows over them)")
        n = sum(1 for l in self.links if {l.a, l.b} == {a, b} and l.role == "data")
        if n < 2:
            return f"{a} and {b} have {n} data cable{'s' if n != 1 else ''} between them: it takes at least two"
        return None

    def aggregatable(self) -> list[tuple[str, str, int]]:
        """Parallel spans that could be bundled but aren't: (a, b, cables), switch first."""
        counts: dict[frozenset, int] = {}
        for l in self.links:
            if l.role == "data" and l.a != l.b:
                pair = frozenset((l.a, l.b))
                counts[pair] = counts.get(pair, 0) + 1
        out = []
        for pair, n in counts.items():
            a, b = sorted(pair, key=lambda d: (d[0] != "s", d[0], int(d[1:])))
            if pair not in self.lags and self.lag_problem(a, b) is None:
                out.append((a, b, n))
        return sorted(out, key=lambda t: (t[0][0], int(t[0][1:]), t[1][0], int(t[1][1:])))

    def lag_ports(self, dev_id: str, port: int) -> list[int]:
        """All of a device's ports in the same bundle as this one (just [port] if unbundled)."""
        li = self.link_index(dev_id, port)
        pair = self.bundle(self.links[li]) if li is not None else None
        if pair is None:
            return [port]
        return sorted(l.port_of(dev_id) for l in self.links if self.bundle(l) == pair)

    def lag_primary(self, dev_id: str, port: int) -> int:
        """The port that speaks for a bundle on this device (its lowest member port)."""
        return self.lag_ports(dev_id, port)[0]

    def _logical_link(self, dev_id: str, port: int, flow: str | None = None) -> int | None:
        """The cable a port sends on. A bundle member stands for the whole bundle: any
        member that can carry data will do (hashed by flow), so one failure isn't fatal."""
        li = self.link_index(dev_id, port)
        if li is None:
            return None
        pair = self.bundle(self.links[li])
        if pair is None:
            return li
        options = [j for j in self.members(pair) if self.links[j].touches(dev_id)]
        if not options:
            return li
        if flow is None:
            return li if li in options else options[0]
        return flow_pick(options, flow, sorted(pair))

    def usable(self, l: Link) -> bool:
        """Can this cable carry data? Not if it's down, a control link, an end is dead,
        STP is blocking either end, or a broadcast storm has saturated it."""
        return (l.up and not l.control and self.devices[l.a].up and self.devices[l.b].up
                and (l.a, l.pa) not in self.stp_blocked and (l.b, l.pb) not in self.stp_blocked
                and l.a not in self.storming and l.b not in self.storming)

    def segment(self, dev_id: str, port: int) -> list[tuple[str, int]]:
        """Other interfaces in the same broadcast domain (reached through switches only)."""
        li = self._logical_link(dev_id, port)
        if li is None or not self.usable(self.links[li]):
            return []
        mine = set(self.lag_ports(dev_id, port))
        v, vp = self.links[li].other(dev_id)
        if self.devices[v].kind != "s":
            return [(v, self.lag_primary(v, vp))]
        seen, q, out = {v}, deque([v]), []
        while q:
            u = q.popleft()
            for l in self.links:
                if l.touches(u) and self.usable(l):
                    w, wp = l.other(u)
                    if w == dev_id and wp in mine:
                        continue
                    if self.devices[w].kind == "s":
                        if w not in seen:
                            seen.add(w)
                            q.append(w)
                    elif (w, self.lag_primary(w, wp)) not in out:
                        out.append((w, self.lag_primary(w, wp)))   # a bundle shows up as one interface
        return out

    def l2path(self, src: tuple[str, int], dst: tuple[str, int],
               flow: str | None = None) -> list[tuple[str, int]] | None:
        """Frame path from one interface to another, switching only through switches.
        Hops across a link aggregation use the member chosen by hashing the flow."""
        key = flow or f"{src}>{dst}"
        path = self._l2path(src, dst, key)
        if path is None:
            return path
        out = list(path)
        for k in range(1, len(path)):              # every hop over a bundle: the same flow hash
            dev, li = path[k]
            pair = self.bundle(self.links[li])
            if pair:
                options = self.members(pair)
                if options:
                    out[k] = (dev, flow_pick(options, key, sorted(pair)))
        return out

    def _l2path(self, src: tuple[str, int], dst: tuple[str, int],
                flow: str | None = None) -> list[tuple[str, int]] | None:
        a, ap = src
        li = self._logical_link(a, ap, flow)
        if li is None or not self.usable(self.links[li]):
            return None
        arrive = {(dst[0], p) for p in self.lag_ports(*dst)}   # any member of dst's bundle
        v, vp = self.links[li].other(a)
        if (v, vp) in arrive:
            return [(a, -1), (v, li)]
        if self.devices[v].kind != "s":
            return None
        prev: dict[str, tuple[str, int]] = {v: (a, li)}
        q = deque([v])
        while q:
            u = q.popleft()
            for i, l in enumerate(self.links):
                if not l.touches(u) or not self.usable(l):
                    continue
                w, wp = l.other(u)
                if (w, wp) in arrive:
                    out, node = [(w, i)], u
                    while node != a:
                        out.append((node, prev[node][1]))
                        node = prev[node][0]
                    return [(a, -1)] + out[::-1]
                if self.devices[w].kind == "s" and w not in prev:
                    prev[w] = (u, i)
                    q.append(w)
        return None

    def learn(self, src: tuple[str, int], path: list[tuple[str, int]]) -> None:
        """Switches along a frame's path learn the sender's MAC on the ingress port."""
        mac = self.devices[src[0]].mac(src[1])
        for dev_id, li in path[1:]:
            if self.devices[dev_id].kind == "s":
                self.mac_tables.setdefault(dev_id, {})[mac] = self.links[li].port_of(dev_id)


    # ---- graph-only mode -----------------------------------------------
    def tree(self, src: str) -> dict[str, tuple[str, int] | None]:
        """Shortest-path tree ignoring addresses. Computers never forward."""
        prev: dict[str, tuple[str, int] | None] = {src: None}
        q = deque([src] if self.devices[src].up else [])
        while q:
            u = q.popleft()
            if u != src and self.devices[u].kind == "c":
                continue
            for i, l in enumerate(self.links):
                if l.touches(u) and self.usable(l):
                    v = l.other(u)[0]
                    if v not in prev:
                        prev[v] = (u, i)
                        q.append(v)
        return prev

    @staticmethod
    def path_in(tree: dict[str, tuple[str, int] | None], dst: str) -> list[tuple[str, int]] | None:
        if dst not in tree:
            return None
        out, node = [], dst
        while node is not None:
            p = tree[node]
            out.append((node, p[1] if p else -1))
            node = p[0] if p else None
        return out[::-1]
