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

    def usable(self, l: Link) -> bool:
        """Can this cable carry data? Not if it's down, a control link, an end is dead,
        STP is blocking either end, or a broadcast storm has saturated it."""
        return (l.up and not l.control and self.devices[l.a].up and self.devices[l.b].up
                and (l.a, l.pa) not in self.stp_blocked and (l.b, l.pb) not in self.stp_blocked
                and l.a not in self.storming and l.b not in self.storming)

    def segment(self, dev_id: str, port: int) -> list[tuple[str, int]]:
        """Other interfaces in the same broadcast domain (reached through switches only)."""
        li = self.link_index(dev_id, port)
        if li is None or not self.usable(self.links[li]):
            return []
        v, vp = self.links[li].other(dev_id)
        if self.devices[v].kind != "s":
            return [(v, vp)]
        seen, q, out = {v}, deque([v]), []
        while q:
            u = q.popleft()
            for l in self.links:
                if l.touches(u) and self.usable(l):
                    w, wp = l.other(u)
                    if (w, wp) == (dev_id, port):
                        continue
                    if self.devices[w].kind == "s":
                        if w not in seen:
                            seen.add(w)
                            q.append(w)
                    elif (w, wp) not in out:
                        out.append((w, wp))
        return out

    def l2path(self, src: tuple[str, int], dst: tuple[str, int],
               flow: str | None = None) -> list[tuple[str, int]] | None:
        """Frame path from one interface to another, switching only through switches.
        Hops across a link aggregation use the member chosen by hashing the flow."""
        path = self._l2path(src, dst)
        if path is None or len(path) < 4:
            return path
        key = flow or f"{src}>{dst}"
        out = list(path)
        for k in range(2, len(path) - 1):          # first and last hops are fixed by the ports
            dev, li = path[k]
            pair = self.bundle(self.links[li])
            if pair:
                options = self.members(pair)
                if options:
                    out[k] = (dev, flow_pick(options, key, sorted(pair)))
        return out

    def _l2path(self, src: tuple[str, int], dst: tuple[str, int]) -> list[tuple[str, int]] | None:
        a, ap = src
        li = self.link_index(a, ap)
        if li is None or not self.usable(self.links[li]):
            return None
        v, vp = self.links[li].other(a)
        if (v, vp) == dst:
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
                if (w, wp) == dst:
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
