"""The public internet between sites, and NAT on the gateways that face it.

``internet`` names a group whose devices are the public internet (ISP gateways
and routers, public servers). A gateway's WAN ports are the ones with the shortest
cabled path to that group: an uplink through a private router or another NAT
still counts, but a LAN that reaches the internet through a second gateway doesn't. A gateway with a WAN port, outside the internet itself, runs NAT
unless it's in ``nat_off``: traffic from its LAN goes out (as the gateway's WAN
address), but a packet arriving on a WAN port can't be forwarded onto its LAN
unless it's a reply to traffic that went out.

NAT is stateful. A flow leaving through a NAT gateway gets a mapping there: its
inside address and source port, the public address and port it leaves as, the
remote it went to, and an expiry (NAT_IDLE seconds after its last packet). An
easy NAT reuses one public port for an inside flow whatever the remote; a hard
NAT picks a new one per remote. Traffic coming back is addressed to the public
endpoint and translated through the mappings, outermost gateway first; anything
without a live mapping from that remote is dropped. A NAT gateway doesn't
advertise its LAN networks to its WAN side, so the outside has no route in.
"""
from __future__ import annotations

from ..constants import NAT_IDLE
from .base import NetworkBase
from .types import Leg, NetError, fmt


class WanMixin(NetworkBase):
    def public(self) -> set[str]:
        """Devices that are part of the public internet."""
        if self.internet not in self.groups:  # type: ignore[attr-defined]
            return set()
        return set(self.devices_in(self.internet))  # type: ignore[attr-defined]

    def wan_ports(self, dev_id: str) -> set[int]:
        """A gateway's ports that face the internet (none outside a gateway): those with the
        fewest cable hops to a public device, not passing back through the gateway itself.
        It follows the cabling, not what's up, so a failure never turns a LAN into a WAN."""
        dev = self.devices[dev_id]
        public = self.public()
        if dev.kind != "g" or not public or dev_id in public:
            return set()
        dist = self._public_distance(public)
        if dev_id not in dist:
            return set()
        # A neighbour one hop closer to the internet is on a shortest way out (one that
        # can't come back through this gateway, or it wouldn't be closer).
        return {p for p in range(1, dev.nports + 1)
                if (l := self.link_on(dev_id, p)) is not None and not l.control
                and dist.get(l.other(dev_id)[0]) == dist[dev_id] - 1}

    def _public_distance(self, public: set[str]) -> dict[str, int]:
        """Cable hops from every device to the nearest public one (one search, remembered
        for the current query)."""
        memo = self._memo
        if memo is not None and "public_distance" in memo:
            return memo["public_distance"]
        adj: dict[str, list[str]] = {}
        for l in self.links:
            if not l.control:
                adj.setdefault(l.a, []).append(l.b)
                adj.setdefault(l.b, []).append(l.a)
        dist = {d: 0 for d in public}
        frontier = list(public)
        while frontier:
            nxt = []
            for u in frontier:
                for v in adj.get(u, ()):
                    if v not in dist:
                        dist[v] = dist[u] + 1
                        nxt.append(v)
            frontier = nxt
        if memo is not None:
            memo["public_distance"] = dist
        return dist

    def nat_hides(self, router: str, entered: int | None, port: int) -> bool:
        """Is router's port hidden from a router that reaches it by its port ``entered``?
        A NAT gateway's LAN side is, from its WAN side."""
        if entered is None or not self.natting(router):
            return False
        wan = self.wan_ports(router)
        return entered in wan and port not in wan

    # ---- mappings --------------------------------------------------------------
    def ephemeral(self) -> int:
        """A fresh source port for a new flow (49152-65535, wrapping)."""
        self._ports += 1
        return 49152 + (self._ports - 1) % 16384

    NAT_PORTS = range(40000, 65536)

    def nat_crossing(self, legs: list[Leg], i: int) -> tuple[str, str] | None:
        """At the router between legs i and i+1: ("out", r) leaving a NAT gateway's LAN for
        its WAN, ("in", r) the other way, or None."""
        if i + 1 >= len(legs):
            return None
        (_, (r, p_in), _), ((_, p_out), _, _) = legs[i], legs[i + 1]
        if not self.natting(r):
            return None
        wan = self.wan_ports(r)
        if p_in not in wan and p_out in wan:
            return "out", r
        if p_in in wan and p_out not in wan:
            return "in", r
        return None

    def nat_map(self, r: str, p_out: int, source: tuple[tuple[int, int], int],
                remote: str) -> tuple[tuple[tuple[int, int], int], str]:
        """A packet leaving NAT gateway r by its WAN port p_out: map its source (address,
        port) to a public endpoint that no other live flow holds. Returns the translated
        source and what to say about it."""
        public = self.addr_of((r, p_out))  # type: ignore[attr-defined]
        maps = self.nat_table.setdefault(r, [])
        maps[:] = [m for m in maps if m["expires"] > self.clock]
        hard = r in self.nat_hard
        same = [m for m in maps if m["inside"] == source and (not hard or m["remote"] == remote)]
        if same:
            pport = same[0]["public"][1]
        else:
            used = {m["public"][1] for m in maps}
            pport = next((p for p in self.NAT_PORTS if p not in used), None)
            if pport is None:
                raise NetError(f"{r}'s NAT is out of public ports: all {len(self.NAT_PORTS)} are in use")
        for m in same:
            if m["remote"] == remote:
                m["expires"] = self.clock + NAT_IDLE
                break
        else:
            maps.append({"inside": source, "public": (public, pport), "remote": remote,
                         "expires": self.clock + NAT_IDLE})
        note = (f"NAT at {r}: {fmt(source[0])}:{source[1]} leaves as {fmt(public)}:{pport} "
                f"(its WAN address; {'hard' if hard else 'easy'} NAT)")
        return (public, pport), note

    def nat_chain(self, remote: str, inside: tuple[int, int]) -> list[tuple[str, dict]]:
        """The live mappings that carry remote's traffic back to an inside address,
        innermost gateway first ([] if none)."""
        chain: list[tuple[str, dict]] = []
        target: tuple = inside                   # the address, then (address, port) once translated
        while len(chain) < len(self.nat_table) + 1:
            hit = next(((g, m) for g, maps in sorted(self.nat_table.items()) for m in reversed(maps)
                        if (m["inside"] == target if chain else m["inside"][0] == target)
                        and m["remote"] == remote and m["expires"] > self.clock
                        and g in self.devices and self.devices[g].up), None)
            if hit is None:
                break
            chain.append(hit)
            target = hit[1]["public"]
        return chain

    def nat_return(self, src: str, remote: str, inside: tuple[str, int]) -> tuple[list[Leg], list[str]] | None:
        """src sending back on a flow the inside started: to the outermost gateway's public
        endpoint, translated inward one gateway at a time. None without live mappings.
        ``nat_returning`` holds the mappings used, for each gateway to refresh when the
        packet reaches it."""
        chain = self.nat_chain(remote, self.addr_of(inside))  # type: ignore[attr-defined]
        if not chain:
            return None
        self.nat_returning = dict(chain)
        legs: list[Leg] = []
        notes = []
        outer_g, outer_m = chain[-1]
        endpoint = self.resolve(outer_m["public"][0])  # type: ignore[attr-defined]
        if endpoint is None:
            return None
        legs += self.route(src, endpoint)  # type: ignore[attr-defined]
        for i in range(len(chain) - 1, -1, -1):
            g, m = chain[i]
            target = self.resolve(chain[i - 1][1]["public"][0]) if i else inside  # type: ignore[attr-defined]
            if target is None:
                return None
            notes.append(f"NAT at {g}: {fmt(m['public'][0])}:{m['public'][1]} → "
                         f"{fmt(m['inside'][0])}:{m['inside'][1]} (the mapping {fmt(m['inside'][0])}'s flow made)")
            legs += self.route(g, target)  # type: ignore[attr-defined]
        return legs, notes

    def nat_drop(self, src: str, dst: tuple[str, int]) -> str | None:
        """Why src can't reach a private dst: the NAT that stands in the way (None if none)."""
        try:
            legs = self.route(src, dst, nat=False)  # type: ignore[attr-defined]
        except NetError:
            return None
        return self.nat_block(legs)

    def nat_forget(self, gateway: str | None = None) -> None:
        """A gateway's mappings vanish (it restarted); every gateway's if None."""
        if gateway is None:
            self.nat_table = {}
        else:
            self.nat_table.pop(gateway, None)

    def age_nat(self) -> None:
        for g, maps in list(self.nat_table.items()):
            maps[:] = [m for m in maps if m["expires"] > self.clock]
            if not maps:
                del self.nat_table[g]

    def natting(self, dev_id: str) -> bool:
        return dev_id not in self.nat_off and bool(self.wan_ports(dev_id))

    nat_returning: dict[str, dict] = {}

    def nat_block(self, legs: list[Leg]) -> str | None:
        """Why NAT drops this (unsolicited) packet, or None if it gets through.

        A packet crosses a router between two legs: in on the port that ends one leg,
        out on the port that starts the next. In on WAN, out on LAN, at a NAT gateway:
        nobody inside asked for it, so there's no translation to send it to."""
        for (_, (r, p_in), _), ((_, p_out), _, _) in zip(legs, legs[1:]):
            if self.natting(r):
                wan = self.wan_ports(r)
                if p_in in wan and p_out not in wan:
                    dst = legs[-1][1]
                    return (f"dropped by {r}'s NAT: unsolicited inbound traffic from the internet to "
                            f"{fmt(self.addr_of(dst))} {dst[0]} on its LAN "  # type: ignore[attr-defined]
                            f"(nothing inside started this flow; try nat {r} off)")
        return None

    def nat_crossings(self, legs: list[Leg]) -> tuple[list[str], list[str]]:
        """NAT gateways a path leaves through (LAN→WAN) and enters through (WAN→LAN)."""
        out, into = [], []
        for (_, (r, p_in), _), ((_, p_out), _, _) in zip(legs, legs[1:]):
            if self.natting(r):
                wan = self.wan_ports(r)
                if p_in not in wan and p_out in wan:
                    out.append(r)
                elif p_in in wan and p_out not in wan:
                    into.append(r)
        return out, into

    def nat_notes(self, legs: list[Leg]) -> list[str]:
        """Translations on the way out: a LAN source leaves as the gateway's WAN address."""
        notes = []
        source = self.addr_of(legs[0][0]) if legs else None  # type: ignore[attr-defined]
        for (_, (r, p_in), _), ((_, p_out), _, _) in zip(legs, legs[1:]):
            if self.natting(r):
                wan = self.wan_ports(r)
                if p_in not in wan and p_out in wan:
                    public = self.addr_of((r, p_out))  # type: ignore[attr-defined]
                    notes.append(f"NAT at {r}: {fmt(source)} leaves as {fmt(public)} (its WAN address)")
                    source = public               # a second NAT translates what the first produced
        return notes
