"""Layer 2: which cables carry data, broadcast domains, frame paths, MAC learning."""
from __future__ import annotations

import functools
import hashlib
import zlib
from collections import deque
from contextlib import contextmanager

from .base import NetworkBase
from .types import Link


def flow_pick(options: list, flow: str, salt: object = "") -> object:
    """Choose one of several equal options for a flow: stable across runs, the same
    every time for the same flow, spread across different flows (CRC32, not hash())."""
    return options[zlib.crc32(f"{flow}|{salt}".encode()) % len(options)]


def member_pick(options: list[int], links: list, flow: str, salt: object = "") -> int:
    """Choose a bundle member for a flow by rendezvous hashing: each member scores the
    flow, the highest score wins. The same flow keeps its member, and when a member
    fails only the flows it carried move (the others' winners are still there)."""
    def score(j: int) -> int:
        l = links[j]
        key = f"{flow}|{salt}|{l.a}:{l.pa}-{l.b}:{l.pb}".encode()
        return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")   # CRC32 is linear: scores would correlate
    return max(options, key=lambda j: (score(j), -j))


def structure_memo(fn):
    """Remember the network's structure (MLAG domains, bundles and their cables) for one
    call. Routing, flooding and STP ask about it for every cable they look at, and it
    can't change while they run. Liveness (usable()) is never remembered."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.remembering():
            return fn(self, *args, **kwargs)
    return wrapper


def route_query(fn):
    """A route is one synchronous, read-only query. Never keep results across routes."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        previous, self._route_memo = self._route_memo, {}
        try:
            with self.remembering():
                return fn(self, *args, **kwargs)
        finally:
            self._route_memo = previous
    return wrapper


def route_memo(fn):
    """Reuse query results only inside route_query; remembering() alone stays live."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        memo = self._route_memo
        if memo is None:
            return fn(self, *args, **kwargs)
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        if key not in memo:
            memo[key] = fn(self, *args, **kwargs)
        return memo[key]
    return wrapper


class Layer2Mixin(NetworkBase):
    @contextmanager
    def remembering(self):
        """A scope in which the structure is remembered (nothing may change it meanwhile)."""
        if self._memo is not None:
            yield
            return
        self._memo = {}
        try:
            yield
        finally:
            self._memo = None

    def _structure(self) -> tuple[dict[str, str], dict[int, frozenset | None], dict[frozenset, list[int]]]:
        """(switch -> its MLAG domain, cable index -> its bundle, bundle -> its cable indices)."""
        domain_of = {d: g for g in sorted(self.mlag_domains)
                     for d in self.devices_in(g)}  # type: ignore[attr-defined]
        bundle_of: dict[int, frozenset | None] = {}
        cables: dict[frozenset, list[int]] = {}
        for i, l in enumerate(self.links):
            pair = self._bundle_of(l, domain_of)
            bundle_of[i] = pair
            if pair is not None:
                cables.setdefault(pair, []).append(i)
        return domain_of, bundle_of, cables

    def _cached_structure(self):
        if self._memo is None:
            return None
        if "structure" not in self._memo:
            self._memo["structure"] = (self._structure(), {id(l): i for i, l in enumerate(self.links)})
        return self._memo["structure"]

    def _bundle_of(self, l: Link, domain_of: dict[str, str]) -> frozenset[str] | None:
        if l.control:
            return None
        ea, eb = domain_of.get(l.a, l.a), domain_of.get(l.b, l.b)
        if ea == eb:
            return None                                   # a peer link, not a bundle member
        pair = frozenset((ea, eb))
        return pair if pair in self.lags else None

    def end(self, dev_id: str) -> str:
        """What a cable's end counts as for aggregation: the MLAG domain a switch is
        in (the domain acts as one logical switch), else the device itself."""
        if not self.mlag_domains:
            return dev_id
        cached = self._cached_structure()
        if cached is not None:
            return cached[0][0].get(dev_id, dev_id)
        if self.devices[dev_id].kind == "s":
            for d in self.mlag_domains:
                if dev_id in self.devices_in(d):  # type: ignore[attr-defined]
                    return d
        return dev_id

    def is_peer_link(self, l: Link) -> bool:
        """A cable between two switches of the same MLAG domain."""
        return bool(self.mlag_domains) and l.a != l.b and self.end(l.a) == self.end(l.b) != l.a

    def bundle(self, l: Link) -> frozenset[str] | None:
        """The link aggregation a cable belongs to (its pair of ends), if any."""
        if not self.lags:
            return None
        cached = self._cached_structure()
        if cached is not None:
            (_, bundle_of, _), index = cached
            i = index.get(id(l))
            if i is not None:
                return bundle_of[i]
        return self._bundle_of(l, {d: self.end(d) for d in (l.a, l.b)})

    def _bundle_cables(self, pair: frozenset[str]) -> list[int]:
        cached = self._cached_structure()
        if cached is not None:
            return cached[0][2].get(pair, [])
        return [i for i, l in enumerate(self.links) if self.bundle(l) == pair]

    def members(self, pair: frozenset[str]) -> list[int]:
        """Indices of a bundle's cables that can carry data now."""
        return [i for i in self._bundle_cables(pair) if self.usable(self.links[i])]

    def _end_kind(self, end: str) -> str:
        return "s" if end in self.mlag_domains else self.devices[end].kind

    def span_cables(self, a: str, b: str) -> int:
        """Data cables between two ends (devices or MLAG domains)."""
        return sum(1 for l in self.links if l.role == "data"
                   and {self.end(l.a), self.end(l.b)} == {a, b} and self.end(l.a) != self.end(l.b))

    def lag_problem(self, a: str, b: str) -> str | None:
        """Why two ends (devices or MLAG domains) can't be aggregated, or None if they can."""
        if a == b:
            return "a link aggregation joins two different ends"
        for x in (a, b):
            if x not in self.mlag_domains and self.end(x) != x:
                return f"{x} is in MLAG domain {self.end(x)}: aggregate to the domain ({self.end(x)}) instead"
        ka, kb = self._end_kind(a), self._end_kind(b)
        if "s" not in (ka, kb) or not {ka, kb} <= {"s", "r", "g"}:
            return ("a link aggregation needs a switch (or MLAG domain) at one end, and a switch, "
                    "router, gateway or domain at the other (between routers, parallel cables are "
                    "routed links and ECMP already spreads flows over them)")
        n = self.span_cables(a, b)
        if n < 2:
            return f"{a} and {b} have {n} data cable{'s' if n != 1 else ''} between them: it takes at least two"
        return None

    @structure_memo
    def aggregatable(self, include_bundled: bool = False) -> list[tuple[str, str, int]]:
        """Parallel spans that could be bundled (but aren't, unless include_bundled)."""
        counts: dict[frozenset, int] = {}
        for l in self.links:
            ea, eb = self.end(l.a), self.end(l.b)
            if l.role == "data" and ea != eb:
                pair = frozenset((ea, eb))
                counts[pair] = counts.get(pair, 0) + 1
        out = []
        for pair, n in counts.items():
            a, b = sorted(pair, key=self._end_order)
            if (include_bundled or pair not in self.lags) and self.lag_problem(a, b) is None:
                out.append((a, b, n))
        return sorted(out, key=lambda t: (self._end_order(t[0]), self._end_order(t[1])))

    def _end_order(self, end: str) -> tuple:
        """Domains first, then switches, then the rest by kind and number."""
        if end in self.mlag_domains:
            return (0, end, 0)
        return (1 if end[0] == "s" else 2, end[0], int(end[1:]))

    def mlag_problem(self, group: str) -> str | None:
        """Why a group can't be an MLAG domain, or None if it can."""
        if group not in self.groups:  # type: ignore[attr-defined]
            return f"no group {group}"
        devs = self.devices_in(group)  # type: ignore[attr-defined]
        if any(self.devices[d].kind != "s" for d in devs):
            return f"an MLAG domain is made of switches only, and {group} has other devices"
        if len(devs) < 2:
            return "an MLAG domain needs at least two switches"
        for other in self.mlag_domains:
            if other != group and set(devs) & set(self.devices_in(other)):  # type: ignore[attr-defined]
                return f"{group} overlaps MLAG domain {other}"
        reach, todo = {devs[0]}, [devs[0]]                 # peer links must join them all
        while todo:
            u = todo.pop()
            for l in self.links:
                if l.touches(u) and l.role == "data" and not l.control:
                    v = l.other(u)[0]
                    if v in devs and v not in reach:
                        reach.add(v)
                        todo.append(v)
        if len(reach) < len(devs):
            loose = [d for d in devs if d not in reach]
            return (f"{group}'s switches must be joined by peer links (cables between them), "
                    f"like vPC: {' '.join(loose)} {'is' if len(loose) == 1 else 'are'} not joined to "
                    f"{devs[0]} — try {devs[0]} {loose[0]}")
        return None

    def lag_ports(self, dev_id: str, port: int) -> list[int]:
        """All of a device's ports in the same bundle as this one (just [port] if unbundled)."""
        if self._memo is not None:
            ports = self._memo.setdefault("lag_ports", {})
            if (dev_id, port) in ports:
                return ports[dev_id, port]
        li = self.link_index(dev_id, port)
        pair = self.bundle(self.links[li]) if li is not None else None
        result = ([port] if pair is None else
                  sorted(self.links[i].port_of(dev_id) for i in self._bundle_cables(pair)
                         if self.links[i].touches(dev_id)))
        if self._memo is not None:
            for p in result:
                ports[dev_id, p] = result
        return result

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
        return member_pick(options, self.links, flow, sorted(pair))

    def usable(self, l: Link) -> bool:
        """Can this cable carry data? Not if it's down, a control link, an end is dead,
        STP is blocking either end, or a broadcast storm has saturated it."""
        return (l.up and not l.control and self.devices[l.a].up and self.devices[l.b].up
                and (l.a, l.pa) not in self.stp_blocked and (l.b, l.pb) not in self.stp_blocked
                and l.a not in self.storming and l.b not in self.storming)

    @route_memo
    @structure_memo
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
            for _, l in self.adjacent_links(u):
                if self.usable(l):
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

    @structure_memo
    def l2path(self, src: tuple[str, int], dst: tuple[str, int],
               flow: str | None = None) -> list[tuple[str, int]] | None:
        """Frame path from one interface to another, switching only through switches.
        Hops across a link aggregation use the member chosen by hashing the flow."""
        key = flow or f"{src}>{dst}"
        path = self._l2path(src, dst, key)
        if path is None:
            return path
        return self._hash_members(path, key)

    def _hash_members(self, path: list[tuple[str, int]], key: str) -> list[tuple[str, int]]:
        out = list(path)
        for k in range(1, len(path)):              # every hop over a bundle: the same flow hash
            dev, li = path[k]
            pair = self.bundle(self.links[li])
            if pair:
                here = {self.links[li].a, self.links[li].b}   # parallel members between these two
                options = [j for j in self.members(pair) if {self.links[j].a, self.links[j].b} == here]
                if options:
                    out[k] = (dev, member_pick(options, self.links, key, sorted(pair)))
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
            for i, l in self.adjacent_links(u):
                if not self.usable(l):
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

    def _toward(self, sw: str, came_by: int, arrive: set[tuple[str, int]]) -> int | None:
        """The cable a flooded frame's copy leaves a switch by on its way to arrive
        (never back the way it came), or None if no copy gets there."""
        first: dict[str, int] = {sw: -1}
        came = self.bundle(self.links[came_by])
        q = deque([sw])
        while q:
            u = q.popleft()
            for i, l in self.adjacent_links(u):
                if i == came_by or not self.usable(l) or (u == sw and came is not None and self.bundle(l) == came):
                    continue
                w, wp = l.other(u)
                if (w, wp) in arrive:
                    return i if u == sw else first[u]
                if self.devices[w].kind == "s" and w not in first:
                    first[w] = i if u == sw else first[u]
                    q.append(w)
        return None

    @structure_memo
    def forward(self, src: tuple[str, int], dst: tuple[str, int],
                flow: str | None = None) -> tuple[list[tuple[str, int]] | None, str]:
        """Follow a unicast frame by the switches' MAC tables: (path, "") or (None, why).

        A switch that knows the destination MAC sends the frame out of that port
        only; one that doesn't floods it, and the copy heading for the destination
        is the one followed. A wrong entry misdelivers or drops the frame. Entries
        on cables that can't carry data count as forgotten (a link-down flush), and
        an MLAG switch whose entry points over its peer link lets the domain, which
        shares one table, pick the way."""
        key = flow or f"{src}>{dst}"
        mac = self.devices[dst[0]].mac(dst[1])
        arrive = {(dst[0], p) for p in self.lag_ports(*dst)}
        li = self._logical_link(src[0], src[1], key)
        if li is None or not self.usable(self.links[li]):
            return None, f"{src[0]}'s cable can't carry data"
        path, u, seen = [(src[0], -1)], src[0], set()
        while True:
            v, vp = self.links[li].other(u)
            path.append((v, li))
            if (v, vp) in arrive:
                return self._hash_members(path, key), ""
            if self.devices[v].kind != "s":
                return None, f"the frame for {mac} reached {v} instead (it drops it)"
            if (v, li) in seen:
                return None, f"the frame for {mac} loops through {v} (stale MAC tables)"
            seen.add((v, li))
            entry = self.mac_tables.get(v, {}).get(mac)
            out = self.link_index(v, entry) if entry is not None else None
            if out is not None and (not self.usable(self.links[out]) or self.is_peer_link(self.links[out])):
                out = None
            if out is not None:
                came = self.bundle(self.links[li])
                if out == li or (came is not None and came == self.bundle(self.links[out])):
                    return None, (f"{v}'s MAC table has {mac} on port {entry}, where the frame came in "
                                  f"(it filters it)")
            else:
                out = self._toward(v, li, arrive)
                if out is None:
                    return None, f"{v} floods the frame for {mac}, but no copy reaches it"
            u, li = v, out

    def learn(self, src: tuple[str, int], path: list[tuple[str, int]]) -> None:
        """Switches along a frame's path learn the sender's MAC on the ingress port."""
        mac = self.devices[src[0]].mac(src[1])
        for dev_id, li in path[1:]:
            if self.devices[dev_id].kind == "s":
                self.mac_tables.setdefault(dev_id, {})[mac] = self.links[li].port_of(dev_id)


    # ---- graph-only mode -----------------------------------------------
    @structure_memo
    def tree(self, src: str) -> dict[str, tuple[str, int] | None]:
        """Shortest-path tree ignoring addresses. Computers never forward."""
        prev: dict[str, tuple[str, int] | None] = {src: None}
        q = deque([src] if self.devices[src].up else [])
        while q:
            u = q.popleft()
            if u != src and self.devices[u].kind == "c":
                continue
            for i, l in self.adjacent_links(u):
                if self.usable(l):
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
