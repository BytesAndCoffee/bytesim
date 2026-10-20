"""Groups: named, nestable sets of devices and other groups."""
from __future__ import annotations

from ..constants import AUTO, BOX_H, GROUP_NAME, REF
from .base import NetworkBase
from .layout import ORIENTATIONS
from .types import NetError


class GroupsMixin(NetworkBase):
    # A group's members are device ids and/or other group names (never ambiguous:
    # group names can't look like device ids). Membership is a tree.

    def set_group_layout(self, name: str, orientation: str) -> None:
        """Orient the selected layout inside a group's frame."""
        if name not in self.groups:
            raise NetError(f"no such group: {name}")
        if orientation not in ORIENTATIONS:
            raise NetError("group layout must be auto, horizontal or vertical")
        if orientation == "auto":
            self.group_layouts.pop(name, None)
        else:
            self.group_layouts[name] = orientation

    def parent_of(self, member: str) -> str | None:
        return next((g for g, ms in self.groups.items() if member in ms), None)

    def group_of(self, dev_id: str) -> str | None:
        """The innermost group holding a device."""
        return self.parent_of(dev_id)

    def chain(self, member: str) -> list[str]:
        """Enclosing groups, outermost first."""
        out, g = [], self.parent_of(member)
        while g is not None:
            out.append(g)
            g = self.parent_of(g)
        return out[::-1]

    def devices_in(self, name: str) -> list[str]:
        out: list[str] = []
        for m in self.groups.get(name, []):
            out.extend(self.devices_in(m) if m in self.groups else [m])
        return out

    def subgroups(self, name: str) -> list[str]:
        return [m for m in self.groups.get(name, []) if m in self.groups]

    def height(self, name: str) -> int:
        """Levels of nested groups inside this one (0 = only devices)."""
        return max((1 + self.height(g) for g in self.subgroups(name)), default=0)

    def free_group_name(self, prefix: str, reserved: frozenset[str] = frozenset()) -> str:
        """prefix + the lowest number not already taken: fab → fab5 when fab1–fab4 exist.
        ``reserved`` holds names claimed explicitly elsewhere (not created yet)."""
        prefix = prefix.lower()
        if REF.match(f"{prefix}1"):
            raise NetError(f"{prefix}(…) would make names like device ids ({prefix}1)")
        n = 1
        while f"{prefix}{n}" in self.groups or f"{prefix}{n}" in self.devices or f"{prefix}{n}" in reserved:
            n += 1
        name = f"{prefix}{n}"
        if not GROUP_NAME.match(name):
            raise NetError(f"bad group name {name!r} (start with a letter, at most 16 characters)")
        return name

    def resolve_group_name(self, name: str, reserved: frozenset[str] = frozenset()) -> str:
        """A group name, or an auto-number pattern like fab(1) turned into the next free one."""
        m = AUTO.match(name)
        if not m:
            return name.lower()
        if int(m[2]) != 1:
            raise NetError(f"{name}: a group name stands for one group, so write {m[1]}(1)")
        return self.free_group_name(m[1], reserved)

    def group_add(self, name: str, members: list[str]) -> list[str]:
        """Add devices and/or groups to a group (creating it at the top level).
        Returns members that were moved out of another group."""
        if not GROUP_NAME.match(name) or REF.match(name):
            raise NetError(f"bad group name {name!r}: start with a letter and must not look like a device id")
        if name in self.devices:
            raise NetError(f"{name} is a device")
        ids = []
        for m in members:
            if m in self.groups:
                if m == name:
                    raise NetError(f"can't put {name} inside itself")
                if m in self.chain(name):
                    raise NetError(f"can't put {m} inside {name}: {name} is already inside {m}")
                ids.append(m)
            else:
                ids.append(self.get(m).id)
        moved = [m for m in ids if self.parent_of(m) not in (None, name)]
        group = self.groups.setdefault(name, [])
        for m in ids:
            old = self.parent_of(m)
            if old not in (None, name):
                self.groups[old].remove(m)
            if m not in group:
                group.append(m)
        self._drop_from_groups([])
        return moved

    def ungroup(self, name: str, members: list[str] | None = None) -> int:
        """Dissolve a group, or take some members out; either way they move up to its parent."""
        if name not in self.groups:
            raise NetError(f"no such group: {name}")
        parent = self.parent_of(name)
        if members is None:
            kids = self.groups.pop(name)
            if parent:
                at = self.groups[parent].index(name)
                self.groups[parent][at:at + 1] = kids
            self.clusters.pop(name, None)
            self.cluster_modes.pop(name, None)
            self.group_layouts.pop(name, None)
            self.mlag_domains.discard(name)
            self.lags = {p for p in self.lags if name not in p}
            self._drop_from_groups([])
            return len(kids)
        ids = [m if m in self.groups else self.get(m).id for m in members]
        out = [m for m in self.groups[name] if m in ids]
        self.groups[name] = [m for m in self.groups[name] if m not in ids]
        if parent:
            self.groups[parent].extend(out)
        self._drop_from_groups([])
        return len(out)

    def _drop_from_groups(self, ids: list[str]) -> None:
        """Remove members everywhere, then prune groups left empty (and their references)."""
        gone = set(ids)
        while True:
            for g in self.groups:
                self.groups[g] = [m for m in self.groups[g] if m not in gone]
            empty = {g for g, ms in self.groups.items() if not ms}
            if not empty:
                break
            for g in empty:
                del self.groups[g]
            gone = empty
        self.clusters = {c: pol for c, pol in self.clusters.items() if c in self.groups}
        self.cluster_modes = {c: m for c, m in self.cluster_modes.items() if c in self.clusters}
        self.group_layouts = {g: m for g, m in self.group_layouts.items() if g in self.groups}
        gone = self.mlag_domains - set(self.groups)
        if gone:
            self.mlag_domains -= gone
            self.lags = {p for p in self.lags if not p & gone}

    def group_bounds(self, name: str) -> tuple[int, int, int, int]:
        """Frame rectangle (x0, y0, x1, y1), inclusive. Outer groups get wider margins
        so nested frames sit inside each other."""
        ds = [self.devices[d] for d in self.devices_in(name)]
        h = self.height(name)
        return (min(d.x for d in ds) - 2 - 2 * h, min(d.y for d in ds) - 1 - h,
                max(d.x + d.width for d in ds) + 1 + 2 * h, max(d.y for d in ds) + BOX_H + h)
