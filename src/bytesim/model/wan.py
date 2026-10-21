"""The public internet between sites, and NAT on the gateways that face it.

``internet`` names a group whose devices are the public internet (ISP gateways
and routers, public servers). A gateway's WAN ports are the ones with the shortest
cabled path to that group: an uplink through a private router or another NAT
still counts, but a LAN that reaches the internet through a second gateway doesn't. A gateway with a WAN port, outside the internet itself, runs NAT
unless it's in ``nat_off``: traffic from its LAN goes out (as the gateway's WAN
address), but a packet arriving on a WAN port can't be forwarded onto its LAN
unless it's a reply to traffic that went out.
"""
from __future__ import annotations

from .base import NetworkBase
from .types import Leg, fmt


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

    def advertises(self, router: str, iface: tuple[str, int], toward: str) -> bool:
        """Does router's routing reach past it for a router arriving by iface? (NAT: see below.)"""
        return True

    def reveals(self, router: str, port: int, asker: str) -> bool:
        """Does router advertise the network on its port to asker? (NAT: see below.)"""
        return True

    def natting(self, dev_id: str) -> bool:
        return dev_id not in self.nat_off and bool(self.wan_ports(dev_id))

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
