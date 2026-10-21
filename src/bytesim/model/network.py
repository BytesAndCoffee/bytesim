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
from .layout import LAYOUT_CHOICES, LayoutMixin
from .naming import NamingMixin
from .routing import RoutingMixin
from .stp import SpanningTreeMixin
from .types import Device, Iface, Link, NetError
from .overlay import OverlayMixin
from .wan import WanMixin


class Network(GroupsMixin, Layer2Mixin, AddressingMixin, RoutingMixin, HAMixin, LayoutMixin,
              NamingMixin, SpanningTreeMixin, CloningMixin, WanMixin, OverlayMixin):
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
        self.flush_ports({e for l in self.links if l.touches(dev.id) for e in ((l.a, l.pa), (l.b, l.pb))})
        self.links = [l for l in self.links if not l.touches(dev.id)]
        del self.devices[dev.id]
        self._drop_from_groups([dev.id])
        for key in [k for k in self.ifaces if k.split(":")[0] == dev.id]:
            del self.ifaces[key]
        self.dhcp_off.discard(dev.id)
        self.nat_off.discard(dev.id)
        self.nat_hard.discard(dev.id)
        self.relays = [d for d in self.relays if d != dev.id]
        for name, ov in self.overlays.items():
            self.leave_overlay(name, dev.id)
            if ov.get("coord") == dev.id:
                del ov["coord"]
            if "acl" in ov:                   # an emptied list still means rules are on: deny all
                ov["acl"] = [r for r in ov["acl"] if dev.id not in r]
        self.lags = {p for p in self.lags if dev.id not in p}
        self.stp_off.discard(dev.id)
        self.stp_priority.pop(dev.id, None)
        self.storming.discard(dev.id)
        self.mac_flaps.pop(dev.id, None)
        self.forget_macs({dev.id})
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
        gone = [l for l in self.links if match(l)]
        self.links = [l for l in self.links if not match(l)]
        self.flush_ports({e for l in gone for e in ((l.a, l.pa), (l.b, l.pb))})   # link down at both ends
        return before - len(self.links)


    def advance(self, seconds: float) -> list[str]:
        """Let simulated time pass. Returns notes on what expired along the way."""
        self.clock += seconds
        self.age_macs()
        return []

    # ---- persistence ---------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({
            "layout_mode": self.layout_mode,
            "group_orientation": self.group_orientation,
            "counters": self.counters,
            "devices": [asdict(d) for d in self.devices.values()],
            "links": [asdict(l) for l in self.links],
            "groups": self.groups,
            "group_layouts": self.group_layouts,
            "ifaces": {k: asdict(v) for k, v in self.ifaces.items()},
            "dhcp_off": sorted(self.dhcp_off),
            "clusters": self.clusters,
            "cluster_modes": self.cluster_modes,
            "lags": sorted(sorted(p, key=self._end_order) for p in self.lags),
            "mlag_domains": sorted(self.mlag_domains),
            "stp_off": sorted(self.stp_off, key=lambda d: (d[0], int(d[1:]))),
            "stp_priority": self.stp_priority,
            "internet": self.internet,
            "nat_off": sorted(self.nat_off, key=lambda d: int(d[1:])),
            "nat_hard": sorted(self.nat_hard, key=lambda d: int(d[1:])),
            "overlays": self.overlay_state(),
            "relays": self.relays,
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Network":
        data = json.loads(raw)
        net = cls()
        for field, choices in LAYOUT_CHOICES.items():
            value = data.get(field, getattr(net, field))
            if value not in choices:
                label = "layout" if field == "layout_mode" else "orientation"
                raise NetError(f"{label} must be one of: {', '.join(choices)}")
            setattr(net, field, value)
        net.counters.update(data["counters"])
        for d in data["devices"]:
            dev = Device(**d)
            net.devices[dev.id] = dev
        net.links = [Link(**l) for l in data["links"]]
        names = set(data.get("groups", {}))
        net.groups = {g: [d for d in ids if d in net.devices or d in names]
                      for g, ids in data.get("groups", {}).items()}
        net._drop_from_groups([])
        for g, orientation in data.get("group_layouts", {}).items():
            if g in net.groups:
                net.set_group_layout(g, orientation)
        for k, v in data.get("ifaces", {}).items():
            if k.split(":")[0] in net.devices:
                try:
                    net.ifaces[k] = Iface(**v)
                except TypeError:
                    pass          # address from an older format: re-learned on load
        net.dhcp_off = set(data.get("dhcp_off", [])) & set(net.devices)
        net.clusters = {c: p for c, p in data.get("clusters", {}).items() if c in net.groups}
        net.cluster_modes = {c: m for c, m in data.get("cluster_modes", {}).items() if c in net.clusters}
        net.mlag_domains = {g for g in data.get("mlag_domains", []) if g in net.groups}
        net.lags = {frozenset(p) for p in data.get("lags", [])
                    if len(set(p)) == 2 and set(p) <= set(net.devices) | net.mlag_domains}
        net.stp_off = set(data.get("stp_off", [])) & set(net.devices)
        net.stp_priority = {d: int(v) for d, v in data.get("stp_priority", {}).items() if d in net.devices}
        net.internet = data.get("internet") if data.get("internet") in net.groups else None
        net.nat_off = {d for d in data.get("nat_off", []) if d in net.devices and net.devices[d].kind == "g"}
        net.nat_hard = {d for d in data.get("nat_hard", []) if d in net.devices and net.devices[d].kind == "g"}
        net.load_overlays(data.get("overlays", {}))
        net.relays = [d for d in data.get("relays", []) if d in net.devices]
        return net
