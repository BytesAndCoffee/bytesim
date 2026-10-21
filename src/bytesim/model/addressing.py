"""Addresses as (network, 8-bit host); router autoconfig, DHCP and ad-hoc."""
from __future__ import annotations

from ..constants import HOST_MAX, POOL, OVERLAY_NETS, VIP_HOST
from .base import NetworkBase
from .layer2 import route_memo, structure_memo
from .types import Iface, NetError, fmt

class AddressingMixin(NetworkBase):
    def iface(self, dev_id: str, port: int) -> Iface:
        key = f"{dev_id}:{port}"
        if key not in self.ifaces:
            self.ifaces[key] = Iface("dhcp" if self.devices[dev_id].kind == "c" else "auto")
        return self.ifaces[key]

    def addr_of(self, ifc: tuple[str, int]) -> tuple[int, int] | None:
        i = self.ifaces.get(f"{ifc[0]}:{ifc[1]}")
        return i.addr if i else None

    @route_memo
    def addressed(self, dev_id: str) -> list[tuple[int, Iface]]:
        dev = self.devices[dev_id]
        if dev.kind == "s":
            return []
        return [(p, self.iface(dev_id, p)) for p in range(1, dev.nports + 1)
                if self.iface(dev_id, p).addr]

    def hosts_on(self, net: int) -> set[int]:
        return {i.host for i in self.ifaces.values() if i.net == net and i.host is not None}

    def resolve(self, addr: tuple[int, int]) -> tuple[str, int] | None:
        if addr[1] == VIP_HOST and (owners := self.vip_owners(addr[0])):
            return owners[0]
        for key, i in self.ifaces.items():
            if i.addr == addr:
                d, p = key.split(":")
                return d, int(p)
        return None

    def local_net(self, nic: tuple[str, int]) -> int | None:
        """Network id already in use on this broadcast domain (routers' first)."""
        seg = sorted(self.segment(*nic), key=lambda e: not self.is_router(e[0]))
        return next((self.addr_of(e)[0] for e in seg if self.addr_of(e)), None)

    def new_net(self) -> int:
        used = {i.net for i in self.ifaces.values() if i.net is not None}
        return next(n for n in range(1, 10**6) if n not in used and n not in OVERLAY_NETS)

    def autoconf_routers(self) -> list[str]:
        """Give every cabled router/gateway port an address on its local network,
        opening a new network id if the segment doesn't have one yet."""
        notes = []
        owner = self._network_owners()
        for dev in self.sorted_devices():
            if dev.kind not in "rg":
                continue
            for p in range(1, dev.nports + 1):
                ifc = self.iface(dev.id, p)
                if ifc.mode != "auto":
                    continue
                li = self.link_index(dev.id, p)
                if li is None or self.links[li].control:
                    ifc.clear()          # unplugged, or a control link: no data address
                    continue
                if p != self.lag_primary(dev.id, p):  # type: ignore[attr-defined]
                    ifc.clear()          # a bundle is one interface: its primary port has the address
                    continue
                if not self.usable(self.links[self._logical_link(dev.id, p)]):  # type: ignore[attr-defined]
                    continue             # failed: keep the config for when it comes back
                here = self._segment_key((dev.id, p))
                if ifc.addr and owner.get(ifc.net, here) != here:
                    ifc.clear()          # a stale id that belongs to another segment
                local = self.local_net((dev.id, p))
                if local is not None and owner.get(local, here) != here:
                    local = None         # this segment's only network id is someone else's
                if ifc.addr and (local is None or local == ifc.net):
                    continue
                net = local if local is not None else self.new_net()
                used = self.hosts_on(net)
                ifc.net, ifc.gw, ifc.via = net, None, "auto"
                ifc.host = next(h for h in range(1, HOST_MAX) if h not in used)
                notes.append(f"{dev.id}:{dev.port_name(p)} → {fmt(ifc.addr)}")
        return notes

    def _segment_key(self, ifc: tuple[str, int]) -> frozenset:
        return frozenset(self.segment(*ifc)) | {ifc}  # type: ignore[attr-defined]

    def _network_owners(self) -> dict[int, frozenset]:
        """Which segment each network id belongs to. A network id is one segment's;
        if a stale id (say, after re-cabling) turns up on several segments, the first
        router port that has it (in device order) keeps it and the others are renumbered."""
        owner: dict[int, frozenset] = {}
        for dev in self.sorted_devices():
            if dev.kind not in "rg":
                continue
            for p in range(1, dev.nports + 1):
                ifc = self.ifaces.get(f"{dev.id}:{p}")
                li = self._logical_link(dev.id, p)  # type: ignore[attr-defined]
                if ifc is None or not ifc.addr or li is None or not self.usable(self.links[li]):
                    continue
                owner.setdefault(ifc.net, self._segment_key((dev.id, p)))
        return owner

    def dhcp_server(self, client: tuple[str, int]) -> tuple[str, int] | None:
        for e in self.segment(*client):
            if self.is_router(e[0]) and e[0] not in self.dhcp_off and self.addr_of(e):
                return e
        return None

    def _free_host(self, net: int, ifc: Iface) -> int:
        used = self.hosts_on(net) - ({ifc.host} if ifc.net == net else set())
        if ifc.net == net and ifc.host in POOL and ifc.host not in used:
            return ifc.host        # keep the number we already had on this network
        host = next((h for h in POOL if h not in used), None)
        if host is None:
            raise NetError(f"network {net} is full (no free 8-bit host numbers)")
        return host

    def lease(self, client: tuple[str, int]) -> tuple[str, tuple[str, int] | None]:
        """DHCP for a client NIC (falls back to ad-hoc). Returns (outcome, server)."""
        ifc = self.iface(*client)
        server = self.dhcp_server(client)
        if server is None:
            self.adhoc(client)
            return "adhoc", None
        snet, _ = self.addr_of(server)
        gw = self.expected_gw(server)
        if ifc.via == "dhcp" and ifc.net == snet and ifc.gw == gw:
            return "renew", server
        ifc.host = self._free_host(snet, ifc)
        ifc.net, ifc.gw, ifc.via = snet, gw, "dhcp"
        return "lease", server

    def expected_gw(self, server: tuple[str, int]) -> int:
        """Gateway a DHCP server hands out: its own host, or its cluster's virtual one."""
        return VIP_HOST if self.cluster_of(server[0]) else self.addr_of(server)[1]

    def adhoc(self, nic: tuple[str, int]) -> None:
        """Take an unused host number on the local network, no server involved."""
        ifc = self.iface(*nic)
        local = self.local_net(nic)
        if ifc.via == "adhoc" and ifc.addr and local in (None, ifc.net):
            return
        net = local if local is not None else self.new_net()
        ifc.host = self._free_host(net, ifc)
        ifc.net, ifc.gw, ifc.via = net, None, "adhoc"

    @structure_memo
    def reconcile(self, skip: frozenset[tuple[str, int]] = frozenset()) -> list[str]:
        """Bring every address in line with the current cabling. Returns what changed.

        STP runs first (it decides which cables forward). Router ports
        autoconfigure; unplugged client NICs release; DHCP clients
        (re)lease when a server is reachable, keep a lease when none is, and fall
        back to ad-hoc when they have nothing. ``skip`` leaves some NICs alone
        (the UI animates their DHCP exchange itself).
        """
        notes = self.stp_update()  # type: ignore[attr-defined]
        self.flush_macs()  # type: ignore[attr-defined]
        notes += [f"addr {n}" for n in self.autoconf_routers()]
        for d in self.sorted_devices():
            if d.kind != "c":
                continue
            for p in range(1, d.nports + 1):
                nic, ifc = (d.id, p), self.iface(d.id, p)
                name = f"{d.id}:{d.port_name(p)}"
                if self.link_index(d.id, p) is None:
                    if ifc.mode in ("dhcp", "adhoc") and ifc.addr:
                        notes.append(f"{name} link down: released {fmt(ifc.addr)}")
                        ifc.clear()
                    continue
                if nic in skip:
                    continue
                old = ifc.addr
                try:
                    if ifc.mode == "adhoc":
                        self.adhoc(nic)
                    elif ifc.mode == "dhcp":
                        server = self.dhcp_server(nic)
                        if server is None:
                            # Nobody to ask: keep a lease we hold, else go ad-hoc.
                            if not ifc.addr or ifc.via == "adhoc":
                                self.adhoc(nic)
                        elif (ifc.via != "dhcp" or ifc.net != self.addr_of(server)[0]
                              or ifc.gw != self.expected_gw(server)):
                            self.lease(nic)
                except NetError as e:
                    notes.append(f"{name}: {e}")
                if ifc.addr != old:
                    notes.append(f"{name} → {fmt(ifc.addr)} via {ifc.via}")
        notes += self._gratuitous_arps()
        return notes

    def _gratuitous_arps(self) -> list[str]:
        """A computer whose link has just come up announces itself (gratuitous ARP), so
        switches learn where it now is. Into a live loop, that sets off a storm."""
        up = {(d.id, p, self.links[li].other(d.id)) for d in self.devices.values() if d.kind == "c" and d.up
              for p, _ in self.addressed(d.id)
              if (li := self._logical_link(d.id, p)) is not None and self.usable(self.links[li])}  # type: ignore[attr-defined]
        before, self._nics_up = self._nics_up, up       # keyed by what's at the other end: a move counts
        notes = []
        for d, p, _ in sorted(up - before) if before is not None else []:
            nic = (d, p)
            fl = self.flood(nic)  # type: ignore[attr-defined]
            if fl.storm:
                self.start_storm(fl)  # type: ignore[attr-defined]
                notes.append(f"{nic[0]}'s gratuitous ARP set off a broadcast storm on "
                             f"{' '.join(sorted(fl.storm_switches, key=self.bridge_id))}")  # type: ignore[attr-defined]
            else:
                self.learn_flood(fl)  # type: ignore[attr-defined]
        return notes
