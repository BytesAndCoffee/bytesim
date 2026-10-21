"""Cloning a group: its devices, nested groups and internal cables."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..constants import GROUP_NAME, RANK, REF
from .base import NetworkBase
from .types import Device, Iface, Link, NetError


@dataclass
class CloneResult:
    group: str                          # the new top-level group
    devices: dict[str, str]             # old device id -> new device id
    groups: dict[str, str]              # old group name -> new group name
    cables: int                         # internal cables copied
    skipped: int                        # cables to the outside, not copied
    rewired: int = 0                    # outside cables re-made onto new endpoints
    moved: list[str] | None = None      # "r3:p6→p7": ports that couldn't be kept


class CloningMixin(NetworkBase):
    def next_group_name(self, base: str, reserved: frozenset[str] = frozenset()) -> str:
        """A free group name like base: fab1 → fab2 (or fab3…), core → core-2."""
        m = re.match(r"^(.*?)(\d+)$", base)
        stem, n, sep = (m[1], int(m[2]), "") if m else (base, 1, "-")
        while True:
            n += 1
            name = f"{stem}{sep}{n}"[-16:]
            if (name not in self.groups and name not in self.devices and name not in reserved
                    and not REF.match(name)):
                return name

    def endpoint_map(self, pairs: list[tuple[str, str]], group: str) -> dict[str, str]:
        """Old outside endpoint → new one, from (old, new) pairs of groups or devices.

        Group members are matched by position, so the two sides need the same size.
        Neither side may be inside the group being cloned."""
        inside = set(self.devices_in(group))  # type: ignore[attr-defined]

        def expand(ref: str) -> list[str]:
            ref = ref.lower()
            if ref in self.groups:
                return self.devices_in(ref)    # type: ignore[attr-defined]
            if ref in self.devices:
                return [ref]
            raise NetError(f"{ref} is neither a group nor a device")

        mapping: dict[str, str] = {}
        for old, new in pairs:
            a, b = expand(old), expand(new)
            if len(a) != len(b):
                raise NetError(f"{old} has {len(a)} devices but {new} has {len(b)}: they're matched one to one")
            for x, y in zip(a, b):
                if x in inside or y in inside:
                    raise NetError(f"{x if x in inside else y} is inside {group}: endpoints must be outside it")
                if x in mapping:
                    raise NetError(f"{x} is listed twice")
                if self.devices[x].kind != self.devices[y].kind:
                    raise NetError(f"{x} is a {self.devices[x].name} but {y} is a {self.devices[y].name}")
                mapping[x] = y
        return mapping

    def clone_group(self, name: str, new_name: str | None = None, dx: int = 0, dy: int = 0,
                    rewire: list[tuple[str, str]] | None = None) -> CloneResult:
        """Copy a group: every device in it (nested groups too), every cable with
        both ends inside, the group tree, cluster policies, STP and DHCP-server
        settings. Cables to the outside are not copied, unless ``rewire`` says where
        they go: pairs of (old endpoint, new endpoint), groups or devices, e.g.
        [("rfab1", "rfab2"), ("rfab2", "rfab3")]. A cable from the group to a device
        of the old endpoint is re-made from the copy to the matching device of the
        new one (same port number there if it's free, else the first free one).

        Copies start healthy (nothing failed) and without addresses: interfaces keep
        their mode, except static ones, which would clash, so they become DHCP
        (computers) or automatic (routers). The new group joins the original's
        parent. dx/dy shift the copies' positions.
        """
        if name not in self.groups:
            raise NetError(f"no such group: {name}")
        if new_name is not None:
            if not GROUP_NAME.match(new_name) or REF.match(new_name):
                raise NetError(f"bad group name {new_name!r}: start with a letter and must not look like a device id")
            if new_name in self.groups or new_name in self.devices:
                raise NetError(f"{new_name} already exists")

        members = self.devices_in(name)       # type: ignore[attr-defined]
        inside = set(members)
        ends = self.endpoint_map(rewire or [], name)

        # Check room on every new endpoint before creating anything.
        outside = [l for l in self.links if (l.a in inside) != (l.b in inside)]
        need: dict[str, int] = {}
        for l in outside:
            far = l.b if l.a in inside else l.a
            if far in ends:
                need[ends[far]] = need.get(ends[far], 0) + 1
        for dev, n in need.items():
            free = sum(1 for p in range(1, self.devices[dev].nports + 1) if self.link_on(dev, p) is None)
            if free < n:
                d = self.devices[dev]
                raise NetError(f"{dev} needs {n} free port{'s' if n != 1 else ''} for the copy but has {free} "
                               f"(try `ports {dev} {d.nports + n - free}`)")

        # Devices, in a stable order so new numbers follow the old ones.
        dev_map: dict[str, str] = {}
        for old in sorted(members, key=lambda d: (RANK[d[0]], int(d[1:]))):
            src: Device = self.devices[old]
            new: Device = self.add(src.kind, src.nports)  # type: ignore[attr-defined]
            new.x, new.y = src.x + dx, src.y + dy
            dev_map[old] = new.id

        # Internal cables keep their ports and roles. Outside ones are re-made onto
        # their new endpoint if one was given, else left behind.
        cables = skipped = rewired = 0
        moved: list[str] = []
        for l in list(self.links):
            if l.a in inside and l.b in inside:
                self.links.append(Link(dev_map[l.a], l.pa, dev_map[l.b], l.pb, l.role))
                cables += 1
            elif l.a in inside or l.b in inside:
                near, near_port = (l.a, l.pa) if l.a in inside else (l.b, l.pb)
                far, far_port = (l.b, l.pb) if l.a in inside else (l.a, l.pa)
                if far not in ends:
                    skipped += 1
                    continue
                target = ends[far]
                port = self._port_like(target, far_port)
                if port != far_port:
                    moved.append(f"{target}:p{far_port}→p{port}")
                self.links.append(Link(dev_map[near], near_port, target, port, l.role))
                rewired += 1

        # The group tree, depth first, so every nested group gets a fresh name.
        group_map: dict[str, str] = {}

        def copy(g: str, forced: str | None) -> str:
            new_g = forced or self.next_group_name(g)
            self.groups[new_g] = []           # reserve the name before naming children
            group_map[g] = new_g
            self.groups[new_g] = [copy(m, None) if m in self.groups else dev_map[m]
                                  for m in list(self.groups[g])]
            if g in self.clusters:
                self.clusters[new_g] = self.clusters[g]
            if g in self.cluster_modes:
                self.cluster_modes[new_g] = self.cluster_modes[g]
            if g in self.group_layouts:
                self.group_layouts[new_g] = self.group_layouts[g]
            return new_g

        top = copy(name, new_name)
        parent = self.parent_of(name)         # type: ignore[attr-defined]
        if parent is not None:
            siblings = self.groups[parent]
            siblings.insert(siblings.index(name) + 1, top)

        # Link aggregations inside the group come along.
        for pair in list(self.lags):
            if pair <= inside:
                self.lags.add(frozenset(dev_map[d] for d in pair))

        # Per-device settings travel with the copy; addresses don't.
        for old, new in dev_map.items():
            if old in self.stp_off:
                self.stp_off.add(new)
            if old in self.stp_priority:
                self.stp_priority[new] = self.stp_priority[old]
            if old in self.dhcp_off:
                self.dhcp_off.add(new)
            if old in self.nat_off:
                self.nat_off.add(new)
            if old in self.nat_hard:
                self.nat_hard.add(new)
            for p in range(1, self.devices[old].nports + 1):
                ifc = self.ifaces.get(f"{old}:{p}")
                if ifc is None:
                    continue
                mode = ifc.mode
                if mode == "static":
                    mode = "dhcp" if self.devices[old].kind == "c" else "auto"
                self.ifaces[f"{new}:{p}"] = Iface(mode)
        return CloneResult(top, dev_map, group_map, cables, skipped, rewired, moved)

    def _port_like(self, dev: str, port: int) -> int:
        """The same port number if it's free on dev, else dev's first free port."""
        if port <= self.devices[dev].nports and self.link_on(dev, port) is None:
            return port
        free = self.free_port(self.devices[dev])
        if free is None:
            raise NetError(f"{dev} has no free ports")
        return free

    def copy_cabling(self, src: str, dst: str, pairs: list[tuple[str, str]]) -> CablingResult:
        """Give group dst the same outside cabling src has to some endpoints, but onto others.

        ``pairs`` are (old endpoint, new endpoint), groups or devices: for every cable
        from a device of src to a device of an old endpoint, a cable is made from the
        matching device of dst to the matching device of the new endpoint. Matching
        is by position, so src and dst (and each endpoint pair) need the same size
        and kinds. No devices are created and src's internal cables are not copied.
        Cables dst already has are counted, so running it twice changes nothing.
        Ports: the same number as the original where free, else the first free one.
        Everything is checked before any cable is made.
        """
        for g in (src, dst):
            if g not in self.groups:
                raise NetError(f"no such group: {g}")
        a, b = self.devices_in(src), self.devices_in(dst)   # type: ignore[attr-defined]
        if len(a) != len(b):
            raise NetError(f"{src} has {len(a)} devices but {dst} has {len(b)}: they're matched one to one")
        match: dict[str, str] = {}
        for x, y in zip(a, b):
            if self.devices[x].kind != self.devices[y].kind:
                raise NetError(f"{x} is a {self.devices[x].name} but {y} is a {self.devices[y].name}")
            match[x] = y
        ends = self.endpoint_map(pairs, src)
        clash = set(ends.values()) & set(b)
        if clash:
            raise NetError(f"{sorted(clash)[0]} is inside {dst}: endpoints must be outside it")

        # The pattern: how many cables (per role) join each src device to each old endpoint.
        wanted: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
        for l in self.links:
            for near, far in ((l.a, l.b), (l.b, l.a)):
                if near in match and far in ends:
                    key = (match[near], ends[far], l.role)
                    wanted.setdefault(key, []).append((l.port_of(near), l.port_of(far)))
        made: list[tuple[str, int, str, int, str]] = []
        already = 0
        for (x, y, role), ports in wanted.items():
            have = sum(1 for l in self.links if l.role == role and {l.a, l.b} == {x, y})
            already += min(have, len(ports))
            for px, py in ports[have:]:
                made.append((x, px, y, py, role))

        need: dict[str, int] = {}
        for x, _, y, _, _ in made:
            need[x] = need.get(x, 0) + 1
            need[y] = need.get(y, 0) + 1
        for dev, n in need.items():
            free = sum(1 for p in range(1, self.devices[dev].nports + 1) if self.link_on(dev, p) is None)
            if free < n:
                nports = self.devices[dev].nports
                raise NetError(f"{dev} needs {n} free port{'s' if n != 1 else ''} but has {free} "
                               f"(try `ports {dev} {nports + n - free}`)")

        moved: list[str] = []
        for x, px, y, py, role in made:
            qx, qy = self._port_like(x, px), self._port_like(y, py)
            moved += [f"{d}:p{p}→p{q}" for d, p, q in ((x, px, qx), (y, py, qy)) if p != q]
            self.links.append(Link(x, qx, y, qy, role))
        if made:
            self.mac_tables = {}
        return CablingResult(len(made), already, moved)


@dataclass
class CablingResult:
    made: int                           # cables created
    already: int                        # cables the pattern wanted that already existed
    moved: list[str]                    # "r3:p6→p7": ports that couldn't mirror the original
