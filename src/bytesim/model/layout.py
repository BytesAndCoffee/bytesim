"""Deterministic network layouts, with room for devices and nested group frames."""
from __future__ import annotations

from collections import deque
from math import cos, pi, sin
from typing import Callable

from ..constants import BOX_H, H_GAP, KINDS, LAYER_GAP, RANK
from .base import NetworkBase
from .types import Device, NetError

LAYOUT_MODES = ("layered", "tree", "circular", "grid", "compact", "compact-layered")
ORIENTATIONS = ("auto", "horizontal", "vertical")
LAYOUT_CHOICES = {"layout_mode": LAYOUT_MODES, "group_orientation": ORIENTATIONS}


class LayoutMixin(NetworkBase):
    def auto_layout(self, width: int, mode: str | None = None, orientation: str | None = None,
                    height: int = 40) -> None:
        """Arrange the physical topology; remember an explicitly selected mode.

        Layered (the default) follows device kinds; tree follows cable depth.
        Circular places blocks around an ellipse, grid packs them into rows,
        and compact packs by height with tighter spacing. Compact-layered uses
        the layered graph ordering with the same tighter spacing.
        Groups stay together in every mode. Failed and control cables still count
        as physical connections, so failure injection doesn't rearrange the view.
        Orientation changes flow without flattening the selected algorithm;
        vertical grid/compact packing wraps at ``height`` (terminal rows).
        """
        mode = self.layout_mode if mode is None else mode
        orientation = self.group_orientation if orientation is None else orientation
        if mode not in LAYOUT_MODES:
            raise NetError(f"layout must be one of: {', '.join(LAYOUT_MODES)}")
        if orientation not in ORIENTATIONS:
            raise NetError(f"orientation must be one of: {', '.join(ORIENTATIONS)}")
        self.layout_mode = mode
        self.group_orientation = orientation
        if not self.devices:
            return
        if mode in ("layered", "tree") and orientation == "auto" and not self.group_layouts:
            self._layered_layout(width, tree=mode == "tree")
        else:
            self._block_layout(max(1, width), max(1, height), mode)

    def _layered_layout(self, width: int, tree: bool = False) -> None:
        adj: dict[str, set[str]] = {d: set() for d in self.devices}
        for l in self.links:
            adj[l.a].add(l.b)
            adj[l.b].add(l.a)

        # Rows follow device type (gateways on top, computers at the bottom);
        # chains of the same type (r1-r2, s1-s2) get extra sub-rows by BFS depth.
        key: dict[str, tuple[int, int]] = {}
        for kind in KINDS:
            same = {d.id for d in self.devices.values() if d.kind == kind}
            roots = sorted((d for d in same
                            if any(RANK[self.devices[n].kind] < RANK[kind] for n in adj[d])),
                           key=lambda d: self.devices[d].num)
            for seed in roots + sorted(same, key=lambda d: self.devices[d].num):
                if seed in key:
                    continue
                key[seed] = (RANK[kind], 0)
                q = deque([seed])
                while q:
                    u = q.popleft()
                    for v in sorted(adj[u] & same):
                        if v not in key:
                            key[v] = (RANK[kind], key[u][1] + 1)
                            q.append(v)
        if tree:
            # A BFS forest rooted at the first gateway/router/switch/computer
            # in each component. Physical links count regardless of live state.
            key = {}
            for dev in self.sorted_devices():
                if dev.id in key:
                    continue
                key[dev.id] = (0, 0)
                q = deque([dev.id])
                while q:
                    u = q.popleft()
                    for v in sorted(adj[u], key=lambda d: (RANK[self.devices[d].kind], self.devices[d].num)):
                        if v not in key:
                            key[v] = (key[u][0] + 1, 0)
                            q.append(v)
        # Devices directly in the same group share a row (the highest any would get).
        # Subgroups keep their own rows; nesting only keeps members side by side.
        for ids in self.groups.values():
            devs = [d for d in ids if d in self.devices]
            if devs:
                top = min(key[d] for d in devs)
                for d in devs:
                    key[d] = top
        layer = key
        chains = {d: self.chain(d) for d in self.devices}
        pad = {g: 2 * (1 + self.height(g)) for g in self.groups}

        def grouped(ids: list[str], keyf: Callable[[int, str], float], depth: int = 0) -> list[str]:
            """Sort a row by keyf, keeping every group's members contiguous, at every level."""
            keys = {d: keyf(i, d) for i, d in enumerate(ids)}
            units: dict[str, list[str]] = {}
            for d in ids:
                c = chains[d]
                units.setdefault(c[depth] if len(c) > depth else d, []).append(d)
            ranked = sorted(units.values(), key=lambda ms: sum(keys[m] for m in ms) / len(ms))
            out: list[str] = []
            for ms in ranked:
                if len(chains[ms[0]]) > depth and len(ms) > 1:   # a group: same rule inside it
                    out += grouped(ms, lambda i, d: keys[d], depth + 1)
                else:
                    out += ms
            return out

        used = sorted(set(layer.values()))
        layers: list[list[str]] = [[] for _ in used]
        for dev in self.sorted_devices():
            layers[used.index(layer[dev.id])].append(dev.id)
        layers = [grouped(ids, lambda i, d: i) for ids in layers]

        # Barycenter sweeps to reduce crossings.
        def sweep(order: range, ref_offset: int) -> None:
            for li in order:
                ref = {d: i for i, d in enumerate(layers[li + ref_offset])}

                def bary(i: int, d: str) -> float:
                    pos = [ref[n] for n in adj[d] if n in ref]
                    return sum(pos) / len(pos) if pos else i

                layers[li] = grouped(layers[li], bary)

        for _ in range(2):
            sweep(range(1, len(layers)), -1)
            sweep(range(len(layers) - 2, -1, -1), 1)

        # Pack the widest row, then pull other boxes toward their neighbours.
        # A group is placed as one block, with extra room for its frame.
        rows = [[self.devices[d] for d in ids] for ids in layers]

        def blocks(devs: list[Device]) -> list[list[Device]]:
            """Runs of devices under the same outermost group move as one block."""
            out: list[list[Device]] = []
            for d in devs:
                root = chains[d.id][:1]
                if out and root and chains[out[-1][0].id][:1] == root:
                    out[-1].append(d)
                else:
                    out.append([d])
            return out

        def gap1(a: Device, b: Device) -> int:
            """Room between neighbours for every frame that closes after a or opens before b."""
            ca, cb = chains[a.id], chains[b.id]
            k = 0
            while k < min(len(ca), len(cb)) and ca[k] == cb[k]:
                k += 1
            return H_GAP + (pad[ca[k]] if len(ca) > k else 0) + (pad[cb[k]] if len(cb) > k else 0)

        def gap(a: list[Device], b: list[Device]) -> int:
            return gap1(a[-1], b[0])

        def offsets(blk: list[Device]) -> list[int]:
            offs, x = [0], 0
            for a, b in zip(blk, blk[1:]):
                x += a.width + gap1(a, b)
                offs.append(x)
            return offs

        def span(devs: list[Device]) -> int:
            return sum(d.width for d in devs) + sum(gap1(a, b) for a, b in zip(devs, devs[1:]))

        widest = max(range(len(rows)), key=lambda i: span(rows[i]))
        for li, devs in enumerate(rows):
            x = max(2, (width - span(devs)) // 2)
            prev = None
            for b in blocks(devs):
                x += gap(prev, b) if prev else 0
                for d, off in zip(b, offsets(b)):
                    d.x, d.y = x + off, 1 + li * (BOX_H + LAYER_GAP)
                x = b[-1].x + b[-1].width
                prev = b
        for _ in range(4):
            for li in sorted(range(len(rows)), key=lambda i: abs(i - widest)):
                if li == widest:
                    continue
                want = []
                for d in rows[li]:
                    # Line our port up with the far end of each cable.
                    nb = []
                    for l in self.links:
                        if l.touches(d.id):
                            o, op = l.other(d.id)
                            od = self.devices[o]
                            if od.y != d.y:
                                nb.append(od.port_x(op) - (d.port_x(l.port_of(d.id)) - d.x))
                    want.append(sum(nb) / len(nb) if nb else d.x)
                wants = dict(zip((d.id for d in rows[li]), want))
                end, prev = -10**6, None
                for b in blocks(rows[li]):
                    offs = offsets(b)
                    w = sum(wants[d.id] - o for d, o in zip(b, offs)) / len(b)
                    x = max(round(w), end + gap(prev, b) if prev else -10**6)
                    for d, o in zip(b, offs):
                        d.x = x + o
                    end, prev = b[-1].x + b[-1].width, b
        self._separate_siblings()
        margin = 2 + max((2 * self.height(g) for g in self.groups), default=0)
        shift = margin - min(d.x for d in self.devices.values())
        if shift > 0:
            for d in self.devices.values():
                d.x += shift

    def _layout_bounds(self, member: str) -> tuple[int, int, int, int]:
        if member in self.groups:
            return self.group_bounds(member)
        d = self.devices[member]
        return d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1

    def _move_layout_member(self, member: str, x: int, y: int) -> None:
        x0, y0, _, _ = self._layout_bounds(member)
        ids = self.devices_in(member) if member in self.groups else [member]
        for did in ids:
            d = self.devices[did]
            d.x += x - x0
            d.y += y - y0

    def _layout_levels(self, members: list[str], sizes: list[tuple[int, int]],
                       tree: bool, h_gap: int, v_gap: int) -> list[tuple[int, int]]:
        """Layer an induced graph, treating each child group as one node."""
        owner = {d: i for i, m in enumerate(members)
                 for d in (self.devices_in(m) if m in self.groups else [m])}
        ranks = [min(RANK[self.devices[d].kind] for d, i in owner.items() if i == n)
                 for n in range(len(members))]
        adj: list[set[int]] = [set() for _ in members]
        for link in self.links:
            a, b = owner.get(link.a), owner.get(link.b)
            if a is not None and b is not None and a != b:
                adj[a].add(b)
                adj[b].add(a)
        order = sorted(range(len(members)), key=lambda i: (ranks[i], i))
        layers: dict[int, tuple[int, int]] = {}
        for rank in ([None] if tree else sorted(set(ranks))):
            same = {i for i in order if rank is None or ranks[i] == rank}
            roots = [i for i in order if i in same and any(ranks[n] < ranks[i] for n in adj[i])]
            for seed in ([] if tree else roots) + [i for i in order if i in same]:
                if seed in layers:
                    continue
                layers[seed] = (0 if tree else ranks[seed], 0)
                q = deque([seed])
                while q:
                    u = q.popleft()
                    for v in sorted(adj[u] & same):
                        if v not in layers:
                            layers[v] = (layers[u][0], layers[u][1] + 1)
                            q.append(v)
        rows = [[i for i in order if layers[i] == key] for key in sorted(set(layers.values()))]
        # Preserve branching while reducing crossings between neighbouring levels.
        for _ in range(2):
            for row in range(1, len(rows)):
                ref = {n: i for i, n in enumerate(rows[row - 1])}
                fallback = {n: i for i, n in enumerate(rows[row])}
                def bary(n: int) -> float:
                    neighbors = [ref[v] for v in sorted(adj[n]) if v in ref]
                    return sum(neighbors) / len(neighbors) if neighbors else fallback[n]
                rows[row].sort(key=bary)
        spans = [sum(sizes[i][0] for i in row) + h_gap * (len(row) - 1) for row in rows]
        out = [(0, 0)] * len(members)
        y = 0
        for row, span in zip(rows, spans):
            x = (max(spans) - span) // 2
            for i in row:
                out[i] = (x, y)
                x += sizes[i][0] + h_gap
            y += max(sizes[i][1] for i in row) + v_gap
        return out

    def _block_layout(self, width: int, height: int, mode: str) -> None:
        """Lay out group subtrees first, then pack their full frame rectangles.

        Measuring actual bounds (including nesting padding) makes groups indivisible
        blocks when arranging their parent. No force simulation or random seed is
        needed, and device port counts may vary freely.
        """
        bounds, move = self._layout_bounds, self._move_layout_member

        def pack(members: list[str], orientation: str) -> None:
            for m in members:
                if m in self.groups:
                    pack(self.groups[m], self.group_layouts.get(m, self.group_orientation))
                else:
                    self.devices[m].x = self.devices[m].y = 0
            if not members:
                return
            # Orient the algorithm's coordinates and block dimensions together;
            # boxes stay upright and the layout keeps its levels, rows or ring.
            layered = mode in ("layered", "tree", "compact-layered")
            transpose = orientation == ("horizontal" if layered else "vertical")
            limit = height if transpose else width
            h_gap, v_gap = (2, 3) if mode in ("compact", "compact-layered") else (H_GAP, LAYER_GAP)
            if transpose:
                h_gap, v_gap = v_gap, h_gap
            sizes = [(bounds(m)[2] - bounds(m)[0] + 1,
                      bounds(m)[3] - bounds(m)[1] + 1) for m in members]
            if transpose:
                sizes = [(h, w) for w, h in sizes]

            def place(member: str, x: int, y: int) -> None:
                move(member, y if transpose else x, x if transpose else y)

            if layered:
                for m, (x, y) in zip(members, self._layout_levels(members, sizes, mode == "tree", h_gap, v_gap)):
                    place(m, x, y)
            elif mode == "compact":
                # Best-fit shelves, tallest blocks first. A shelf's first block
                # sets its height; later blocks fill its remaining width without
                # increasing that height. Ties follow the original member order.
                shelves: list[list[int]] = []  # y, height, next free x
                order = sorted(range(len(members)), key=lambda i: (-sizes[i][1], -sizes[i][0], i))
                for i in order:
                    w, h = sizes[i]
                    fits = [(limit - x - w, row) for row, (_, _, x) in enumerate(shelves)
                            if x + w <= limit]
                    if fits:
                        row = min(fits)[1]
                    else:
                        y = shelves[-1][0] + shelves[-1][1] + v_gap if shelves else 0
                        row = len(shelves)
                        shelves.append([y, h, 0])
                    y, _, x = shelves[row]
                    place(members[i], x, y)
                    shelves[row][2] += w + h_gap
            elif mode == "circular" and len(members) > 1:
                centers = [(cos(2 * pi * i / len(members) - pi / 2),
                            sin(2 * pi * i / len(members) - pi / 2)) for i in range(len(members))]
                pairs = [(i, j) for i in range(len(members)) for j in range(i + 1, len(members))]
                rx = max(1.0, (limit - max(w for w, _ in sizes) - 4) / 2)
                ry = (max(h for _, h in sizes) + LAYER_GAP + 2) / 2
                # Use the canvas width when possible. Equal-height pairs need
                # horizontal room; other close pairs can separate vertically.
                for i, j in pairs:
                    dx = abs(centers[i][0] - centers[j][0])
                    dy = abs(centers[i][1] - centers[j][1])
                    if dy < 1e-8:
                        rx = max(rx, ((sizes[i][0] + sizes[j][0]) / 2 + H_GAP + 2) / dx)
                for i, j in pairs:
                    dx = abs(centers[i][0] - centers[j][0])
                    dy = abs(centers[i][1] - centers[j][1])
                    if rx * dx < (sizes[i][0] + sizes[j][0]) / 2 + H_GAP + 2 and dy >= 1e-8:
                        ry = max(ry, ((sizes[i][1] + sizes[j][1]) / 2 + LAYER_GAP + 2) / dy)
                for m, (w, h), (cx, cy) in zip(members, sizes, centers):
                    place(m, round(rx * cx - w / 2), round(ry * cy - h / 2))
            else:
                x = y = row_height = 0
                for m, (w, h) in zip(members, sizes):
                    if x and x + w > limit:
                        x, y, row_height = 0, y + row_height + LAYER_GAP, 0
                    place(m, x, y)
                    x += w + h_gap
                    row_height = max(row_height, h)

        # Sort devices by kind/id and place each outer group at its first device.
        # Use member order as the deterministic tie-breaker inside groups.
        roots = list(dict.fromkeys((self.chain(d.id) or [d.id])[0]
                                   for d in self.sorted_devices()))
        pack(roots, self.group_orientation)
        left = min(bounds(m)[0] for m in roots)
        top = min(bounds(m)[1] for m in roots)
        for m in roots:
            x0, y0, _, _ = bounds(m)
            move(m, x0 + 2 - left, y0 + 1 - top)

    def _separate_siblings(self) -> None:
        """A group spanning several rows can overlap a sibling on one of them;
        push the right-hand sibling (and its contents) clear, until nothing overlaps."""
        def rect(m: str) -> tuple[int, int, int, int]:
            if m in self.groups:
                return self.group_bounds(m)
            d = self.devices[m]
            return d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1

        def contents(m: str) -> list[Device]:
            return [self.devices[d] for d in (self.devices_in(m) if m in self.groups else [m])]

        levels = [[m for m in list(self.devices) + list(self.groups) if self.parent_of(m) is None]]
        levels += [ms for ms in self.groups.values()]
        for _ in range(50):
            moved = False
            for sibs in levels:
                rs = sorted(((rect(m), m) for m in sibs), key=lambda t: t[0][0])
                for i, (ra, a) in enumerate(rs):
                    for rb, b in rs[i + 1:]:
                        if ra[0] <= rb[0] <= ra[2] + 1 and ra[1] <= rb[3] and rb[1] <= ra[3]:
                            for d in contents(b):
                                d.x += ra[2] + 2 - rb[0]
                            moved = True
                            break
                    if moved:
                        break
                if moved:
                    break
            if not moved:
                return

    def place_new(self, dev: Device) -> None:
        """Drop a new device into free space on its type's row (manual mode)."""
        y = 1 + RANK[dev.kind] * (BOX_H + LAYER_GAP)
        right = [o.x + o.width for o in self.devices.values()
                 if o is not dev and o.y < y + BOX_H + 1 and y < o.y + BOX_H + 1]
        dev.x, dev.y = (max(right) + H_GAP if right else 2), y
