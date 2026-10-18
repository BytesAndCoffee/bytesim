"""The Network: every mixin combined, plus device/cable editing and JSON persistence."""
from __future__ import annotations

import json
from dataclasses import asdict

from ..constants import KINDS, MAX_PORTS
from .addressing import AddressingMixin
from .cloning import CloningMixin
from .groups import GroupsMixin
from .ha import HAMixin
from .layer2 import Layer2Mixin
from .layout import LayoutMixin
from .naming import NamingMixin
from .routing import RoutingMixin
from .stp import SpanningTreeMixin
from .types import Device, Iface, Link, NetError


class Network(GroupsMixin, Layer2Mixin, AddressingMixin, RoutingMixin, HAMixin, LayoutMixin,
              NamingMixin, SpanningTreeMixin, CloningMixin):
    """A simulated network: devices, cables, groups, addresses and HA clusters.

    Pure model, no UI: everything here can be driven from Python or tests.
    """

    def add(self, kind: str, nports: int | None = None) -> Device:
        nports = KINDS[kind][1] if nports is None else nports
        if not 1 <= nports <= MAX_PORTS:
            raise NetError(f"port count must be 1..{MAX_PORTS}")
        self.counters[kind] += 1
        dev = Device(kind, self.counters[kind], nports)
        self.devices[dev.id] = dev
        return dev

    def remove(self, dev_id: str) -> int:
        dev = self.get(dev_id)
        before = len(self.links)
        self.links = [l for l in self.links if not l.touches(dev.id)]
        del self.devices[dev.id]
        self._drop_from_groups([dev.id])
        for key in [k for k in self.ifaces if k.split(":")[0] == dev.id]:
            del self.ifaces[key]
        self.dhcp_off.discard(dev.id)
        self.stp_off.discard(dev.id)
        self.stp_priority.pop(dev.id, None)
        self.storming.discard(dev.id)
        self.mac_flaps.pop(dev.id, None)
        self.mac_tables = {}
        return before - len(self.links)

    def resize(self, dev_id: str, nports: int) -> None:
        dev = self.get(dev_id)
        if not 1 <= nports <= MAX_PORTS:
            raise NetError(f"port count must be 1..{MAX_PORTS}")
        used = [l.port_of(dev.id) for l in self.links if l.touches(dev.id)]
        if used and max(used) > nports:
            raise NetError(f"{dev.id} port {max(used)} is in use; unlink it first")
        dev.nports = nports
        for p in range(nports + 1, MAX_PORTS + 1):
            self.ifaces.pop(f"{dev.id}:{p}", None)

    def connect(self, a: str, pa: int | None, b: str, pb: int | None, role: str = "data") -> Link:
        da, db = self.get(a), self.get(b)
        if da is db:
            raise NetError("can't cable a device to itself")
        ports = []
        for dev, p in ((da, pa), (db, pb)):
            if p is None:
                p = self.free_port(dev)
                if p is None:
                    raise NetError(f"{dev.id} has no free ports (try `ports {dev.id} {dev.nports + 1}`)")
            elif not 1 <= p <= dev.nports:
                raise NetError(f"{dev.id} has no port {p} (it has {dev.nports})")
            elif (l := self.link_on(dev.id, p)) is not None:
                o, op = l.other(dev.id)
                raise NetError(f"{dev.id}:{dev.port_name(p)} already cabled to {o}")
            ports.append(p)
        link = Link(da.id, ports[0], db.id, ports[1], role)
        self.links.append(link)
        return link

    def disconnect(self, a: str, pa: int | None, b: str | None) -> int:
        da = self.get(a)
        db = self.get(b) if b else None

        def match(l: Link) -> bool:
            if not l.touches(da.id):
                return False
            if pa is not None and l.port_of(da.id) != pa:
                return False
            return db is None or l.other(da.id)[0] == db.id

        before = len(self.links)
        self.links = [l for l in self.links if not match(l)]
        if len(self.links) != before:
            self.mac_tables = {}      # topology changed: switches forget what they learned
        return before - len(self.links)


    # ---- persistence ---------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({
            "counters": self.counters,
            "devices": [asdict(d) for d in self.devices.values()],
            "links": [asdict(l) for l in self.links],
            "groups": self.groups,
            "ifaces": {k: asdict(v) for k, v in self.ifaces.items()},
            "dhcp_off": sorted(self.dhcp_off),
            "clusters": self.clusters,
            "stp_off": sorted(self.stp_off, key=lambda d: (d[0], int(d[1:]))),
            "stp_priority": self.stp_priority,
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Network":
        data = json.loads(raw)
        net = cls()
        net.counters.update(data["counters"])
        for d in data["devices"]:
            dev = Device(**d)
            net.devices[dev.id] = dev
        net.links = [Link(**l) for l in data["links"]]
        names = set(data.get("groups", {}))
        net.groups = {g: [d for d in ids if d in net.devices or d in names]
                      for g, ids in data.get("groups", {}).items()}
        net._drop_from_groups([])
        for k, v in data.get("ifaces", {}).items():
            if k.split(":")[0] in net.devices:
                try:
                    net.ifaces[k] = Iface(**v)
                except TypeError:
                    pass          # address from an older format: re-learned on load
        net.dhcp_off = set(data.get("dhcp_off", [])) & set(net.devices)
        net.clusters = {c: p for c, p in data.get("clusters", {}).items() if c in net.groups}
        net.stp_off = set(data.get("stp_off", [])) & set(net.devices)
        net.stp_priority = {d: int(v) for d, v in data.get("stp_priority", {}).items() if d in net.devices}
        return net
