"""Automatic arrangement: rows by device type, groups kept together, nested frames."""
from __future__ import annotations

from collections import deque
from typing import Callable

from ..constants import BOX_H, H_GAP, KINDS, LAYER_GAP, RANK
from .base import NetworkBase
from .types import Device


class LayoutMixin(NetworkBase):
    def auto_layout(self, width: int) -> None:
        if not self.devices:
            return
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
