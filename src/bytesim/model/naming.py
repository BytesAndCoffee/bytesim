"""Renumbering devices in traversal order (BFS or DFS over the cabling)."""
from __future__ import annotations

from collections import deque

from ..constants import KINDS, RANK
from .base import NetworkBase
from .types import NetError

ORDERS = ("bfs", "dfs")


class NamingMixin(NetworkBase):
    def default_root(self) -> str | None:
        """Where a traversal starts unless told otherwise: the first gateway,
        else the first router, switch, then computer."""
        devs = self.sorted_devices()
        return devs[0].id if devs else None

    def _neighbours(self, dev_id: str) -> list[str]:
        """Devices cabled to dev_id, in the order of dev_id's own ports.

        Naming follows the physical cabling, so every cable counts (failed or
        control cables too) and computers are walked through like anything else.
        """
        ends = sorted((l.port_of(dev_id), l.other(dev_id)[0]) for l in self.links if l.touches(dev_id))
        return [other for _, other in ends]

    def traversal(self, order: str, root: str | None = None) -> list[str]:
        """Every device id, in the order a BFS or DFS from root first reaches it.

        Neighbours are taken in port order. Devices the root can't reach are
        picked up by starting again from the next one in the old (kind, number)
        order, so the result always covers the whole network.
        """
        if order not in ORDERS:
            raise NetError(f"order must be one of: {', '.join(ORDERS)}")
        root = self.get(root).id if root else self.default_root()
        if root is None:
            return []
        starts = [root] + [d.id for d in self.sorted_devices() if d.id != root]
        seen: set[str] = set()
        out: list[str] = []
        for start in starts:
            if start in seen:
                continue
            if order == "bfs":
                seen.add(start)
                q = deque([start])
                while q:
                    u = q.popleft()
                    out.append(u)
                    for v in self._neighbours(u):
                        if v not in seen:
                            seen.add(v)
                            q.append(v)
            else:
                # Iterative pre-order DFS: visit, then go deep through the first
                # unvisited neighbour before trying the next port.
                seen.add(start)
                out.append(start)
                stack = [iter(self._neighbours(start))]
                while stack:
                    v = next(stack[-1], None)
                    if v is None:
                        stack.pop()
                    elif v not in seen:
                        seen.add(v)
                        out.append(v)
                        stack.append(iter(self._neighbours(v)))
        return out

    def renumber(self, order: str, root: str | None = None) -> dict[str, str]:
        """Renumber each kind 1..n in traversal order. Returns {old id: new id}
        for every device whose id changed.

        Cables, groups, addresses and DHCP settings follow their device. Switch
        MAC tables are cleared, since MACs are derived from device ids. Counters
        are reset so the next new device of each kind gets n+1.
        """
        visit = self.traversal(order, root)
        counts = {k: 0 for k in KINDS}
        new_num: dict[str, int] = {}
        for did in visit:
            dev = self.devices[did]
            counts[dev.kind] += 1
            new_num[did] = counts[dev.kind]
        mapping = {did: f"{self.devices[did].kind}{n}" for did, n in new_num.items()}
        changed = {old: new for old, new in mapping.items() if old != new}
        self.counters = counts
        if not changed:
            return {}

        def ren(did: str) -> str:
            return mapping.get(did, did)

        for did, n in new_num.items():
            self.devices[did].num = n
        self.devices = {d.id: d for d in sorted(self.devices.values(), key=lambda d: (RANK[d.kind], d.num))}
        for l in self.links:
            l.a, l.b = ren(l.a), ren(l.b)
        self.groups = {g: [ren(m) if m in mapping else m for m in ms] for g, ms in self.groups.items()}
        self.ifaces = {f"{ren(k.split(':')[0])}:{k.split(':')[1]}": v for k, v in self.ifaces.items()}
        self.dhcp_off = {ren(d) for d in self.dhcp_off}
        self.nat_off = {ren(d) for d in self.nat_off}
        self.nat_hard = {ren(d) for d in self.nat_hard}
        self.relays = [ren(d) for d in self.relays]
        for ov in self.overlays.values():
            ov["members"] = [ren(d) for d in ov["members"]]
            ov["hosts"] = {ren(d): h for d, h in ov.get("hosts", {}).items()}
            for key in ("coord", "exit"):
                if key in ov:
                    ov[key] = ren(ov[key])
            if "routes" in ov:
                ov["routes"] = [ren(d) for d in ov["routes"]]
            if "acl" in ov:                       # rules name devices, groups, * or internet
                ov["acl"] = [[ren(x) if x in mapping else x for x in r] for r in ov["acl"]]
        self.lags = {frozenset(ren(d) for d in p) for p in self.lags}   # domain names map to themselves
        self.stp_off = {ren(d) for d in self.stp_off}
        self.stp_priority = {ren(d): v for d, v in self.stp_priority.items()}
        self.stp_cost = {(ren(d), p): c for (d, p), c in self.stp_cost.items()}
        self.static_routes = {ren(d): r for d, r in self.static_routes.items()}
        self.nat_table = {ren(g): [{**m, "remote": ren(m["remote"]) if m["remote"] in self.devices else m["remote"]}
                                   for m in maps] for g, maps in self.nat_table.items()}
        self.stp_blocked = {(ren(d), p) for d, p in self.stp_blocked}
        self.stp_roots = {ren(d): ren(r) for d, r in self.stp_roots.items()}
        self.stp_root_ports = {ren(d): p for d, p in self.stp_root_ports.items()}
        self.storming = {ren(d) for d in self.storming}
        self.mac_flaps = {ren(d): f for d, f in self.mac_flaps.items()}
        self.forget_macs()   # MACs are named after device ids
        self.arp_cache = {(ren(d), p): {a: ((ren(o), op), t) for a, ((o, op), t) in cache.items()}
                          for (d, p), cache in self.arp_cache.items()}
        if self._nics_up is not None:
            self._nics_up = {(ren(d), p, (ren(o), op)) for d, p, (o, op) in self._nics_up}
        self.notes = []
        return changed
