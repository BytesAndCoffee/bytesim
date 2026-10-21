"""The network canvas: draws devices, cables, frames and packets; mouse and keys."""
from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding
from textual.strip import Strip
from textual.widget import Widget

from ..constants import BOX_H
from ..model import Device, Network, fmt
from .cables import LINE, LINE2, LINE_DASH, LINE_HEAVY, N, S, dirbit, route
from .theme import HA_COLOR, HEAT, LAG_COLOR, PEER_COLOR, ROLE_COLOR, group_color

@dataclass
class Packet:
    cells: list[tuple[int, int]]
    color: str
    on_done: Callable[[], None] | None = None
    pos: float = 0


TRAIL = 4
ANIMATION_FPS = 60
PACKET_FRAMES = 90       # normal speed: longest active path takes about 1.5 seconds
EFFECT_FPS = 45          # retain existing cell speed and blink/storm timing
FLASH_FRAMES = 40        # two-thirds of a second
WORK_SECONDS = 0.006     # yield routing/callback bursts back to Textual after each slice


class NetCanvas(Widget, can_focus=True):
    BINDINGS = [
        Binding("up", "move(0,-1)", "Move", show=False),
        Binding("down", "move(0,1)", "Move", show=False),
        Binding("left", "move(-1,0)", "Move", show=False),
        Binding("right", "move(1,0)", "Move", show=False),
        Binding("shift+up", "move(0,-4)", show=False),
        Binding("shift+down", "move(0,4)", show=False),
        Binding("shift+left", "move(-4,0)", show=False),
        Binding("shift+right", "move(4,0)", show=False),
        Binding("tab", "cycle(1)", "Next dev"),
        Binding("shift+tab", "cycle(-1)", "Prev dev", show=False),
        Binding("a", "arrange", "Auto-arrange"),
        Binding("u", "undo", "Undo"),
        Binding("U", "redo", "Redo", show=False),
        Binding("v", "toggle_addr", "Addresses"),
        Binding("x,delete", "delete", "Delete"),
        Binding("h", "pan(-4,0)", "Pan hjkl", show=False),
        Binding("j", "pan(0,2)", show=False),
        Binding("k", "pan(0,-2)", show=False),
        Binding("l", "pan(4,0)", show=False),
        Binding("enter,colon,slash,i", "app.focus_cmd", "Command"),
        Binding("escape", "deselect", show=False),
    ]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.selected: str | None = None
        self.selected_group: str | None = None
        self.show_addr = True
        self.pan = (0, 0)
        self.packets: list[Packet] = []
        self._preparing: deque[Iterator[None]] = deque()
        self._callbacks: deque[Callable[[], None]] = deque()
        self._work_scheduled = False
        self._packet_state: tuple | None = None
        self._geom: tuple | None = None          # (layout key, cable geometry)
        self._grid: tuple | None = None          # (size, pan, chars, styles) of the current frame
        self._strips: dict[tuple, Strip] = {}    # row runs -> Strip, reused while a row is unchanged
        self._styles: dict[str, Style] = {}      # style string -> Style over the widget's base
        self._base: Style | None = None
        self.hot_links: Counter[int] = Counter()
        self.flash: dict[str, list] = {}   # device -> [colour, ticks left]
        self._timer = None
        self._drag: tuple | None = None
        self.frame = 0                      # animation clock (storms crawl along their loop)

    def reset(self) -> None:
        """Forget selection and anything in flight (the network was replaced)."""
        self.selected = self.selected_group = None
        self.cancel_packets()
        self.pan = (0, 0)

    def cancel_packets(self) -> None:
        """Discard work whose paths may no longer match the edited network."""
        # Generators hold no contexts across yields. Dropping them also works
        # when a currently executing query discovers a storm and calls changed().
        self._preparing.clear()
        self._callbacks.clear()
        self.packets.clear()
        self.flash.clear()
        self.hot_links.clear()
        self._packet_state = None

    def _state(self) -> tuple:
        return self.net.to_json(), frozenset(self.net.storming), self.app.graph  # type: ignore[attr-defined]

    def cancel_if_changed(self) -> bool:
        if self.busy and self._packet_state != self._state():
            self.cancel_packets()
            return True
        return False

    @property
    def preparing(self) -> bool:
        return bool(self._preparing)

    def prepare(self, steps: Iterator[None]) -> None:
        if not self.busy:
            self._packet_state = self._state()
        self._preparing.append(steps)
        self.animate()
        self.app.refresh_sidebar()  # type: ignore[attr-defined]

    @property
    def busy(self) -> bool:
        """True until preparation, playback, and all completion callbacks finish."""
        return bool(self.packets or self._preparing or self._callbacks)

    @property
    def net(self) -> Network:
        return self.app.net  # type: ignore[attr-defined]

    # ---- selection / movement -----------------------------------------
    def select(self, dev_id: str | None) -> None:
        self.selected = dev_id
        self.selected_group = None
        if dev_id:
            d = self.net.devices[dev_id]
            self.scroll_into_view(d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1)
        self.app.refresh_sidebar()  # type: ignore[attr-defined]
        self.refresh()

    def select_group(self, name: str) -> None:
        self.selected = None
        self.selected_group = name
        self.scroll_into_view(*self.net.group_bounds(name))
        self.app.refresh_sidebar()  # type: ignore[attr-defined]
        self.refresh()

    def scroll_into_view(self, x0: int, y0: int, x1: int, y1: int) -> None:
        ox, oy = self.pan
        w, h = self.size.width or 80, self.size.height or 24
        if x0 < ox:
            ox = x0 - 2
        elif x1 >= ox + w:
            ox = x1 - w + 3
        if y0 < oy:
            oy = y0 - 1
        elif y1 >= oy + h:
            oy = y1 - h + 3
        self.pan = (ox, oy)

    def _moving(self) -> list[Device]:
        if self.selected_group:
            return [self.net.devices[d] for d in self.net.devices_in(self.selected_group)]
        return [self.net.devices[self.selected]] if self.selected else []

    def _shift(self, devs: list[Device], dx: int, dy: int, what: str = "") -> bool:
        """Move devices together without letting any go off the top/left edge."""
        dx = max(dx, -min(d.x for d in devs))
        dy = max(dy, -min(d.y for d in devs))
        if dx or dy:
            self.app.before_move(what or " ".join(d.id for d in devs))  # type: ignore[attr-defined]
        for d in devs:
            d.x, d.y = d.x + dx, d.y + dy
        if dx or dy:
            self.app.manual_moved()  # type: ignore[attr-defined]
        return bool(dx or dy)

    def action_move(self, dx: int, dy: int) -> None:
        devs = self._moving()
        if not devs:
            self.app.log_msg("[dim]select a device or group first (tab, click, or type its name)[/]")  # type: ignore[attr-defined]
            return
        self._shift(devs, dx, dy, self.selected_group or self.selected or "")
        if self.selected_group:
            self.scroll_into_view(*self.net.group_bounds(self.selected_group))
        else:
            d = devs[0]
            self.scroll_into_view(d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1)
        self.app.refresh_sidebar()  # type: ignore[attr-defined]
        self.refresh()

    def action_cycle(self, step: int) -> None:
        devs = [d.id for d in self.net.sorted_devices()]
        if not devs:
            return
        i = devs.index(self.selected) + step if self.selected in devs else (0 if step > 0 else -1)
        self.select(devs[i % len(devs)])

    def action_deselect(self) -> None:
        self.select(None)

    def action_arrange(self) -> None:
        self.app.run_command("auto")  # type: ignore[attr-defined]   (so it can be undone)

    def action_delete(self) -> None:
        if self.selected_group:
            self.app.run_command(f"ungroup {self.selected_group}")  # type: ignore[attr-defined]
        elif self.selected:
            self.app.run_command(f"del {self.selected}")  # type: ignore[attr-defined]

    def action_undo(self) -> None:
        self.app.run_command("undo")  # type: ignore[attr-defined]

    def action_redo(self) -> None:
        self.app.run_command("redo")  # type: ignore[attr-defined]

    def action_toggle_addr(self) -> None:
        self.show_addr = not self.show_addr
        self.refresh()

    def action_pan(self, dx: int, dy: int) -> None:
        self.pan = (self.pan[0] + dx, self.pan[1] + dy)
        self.refresh()

    # ---- mouse ---------------------------------------------------------
    def _hit(self, wx: int, wy: int) -> Device | None:
        for d in reversed(self._draw_order()):
            if d.contains(wx, wy):
                return d
        return None

    def _hit_group(self, wx: int, wy: int) -> str | None:
        """A group is hit by clicking its frame (border or label); inner frames win."""
        for g in sorted(self.net.groups, key=lambda g: -len(self.net.chain(g))):
            x0, y0, x1, y1 = self.net.group_bounds(g)
            on_x = wx in (x0, x1) and y0 <= wy <= y1
            on_y = wy in (y0, y1) and x0 <= wx <= x1
            if on_x or on_y:
                return g
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        off = event.get_content_offset(self)
        if off is None:
            return
        self.focus()
        wx, wy = off.x + self.pan[0], off.y + self.pan[1]
        hit = self._hit(wx, wy)
        if hit:
            self.select(hit.id)
            self._drag = ("dev", hit.id, wx, wy)
        elif g := self._hit_group(wx, wy):
            self.select_group(g)
            self._drag = ("group", g, wx, wy)
        else:
            self._drag = ("pan", off.x, off.y, *self.pan)
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._drag:
            return
        off = event.get_content_offset_capture(self)
        if self._drag[0] in ("dev", "group"):
            kind, name, lx, ly = self._drag
            if kind == "dev":
                devs = [self.net.devices[name]] if name in self.net.devices else []
            else:
                devs = [self.net.devices[d] for d in self.net.devices_in(name)]
            if not devs:
                return
            wx, wy = off.x + self.pan[0], off.y + self.pan[1]
            if self._shift(devs, wx - lx, wy - ly, name):
                self._drag = (kind, name, wx, wy)
                self.refresh()
        else:
            _, sx, sy, ox, oy = self._drag
            self.pan = (ox - (off.x - sx), oy - (off.y - sy))
            self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag:
            self._drag = None
            self.release_mouse()
            self.app.refresh_sidebar()  # type: ignore[attr-defined]

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.action_pan(*((4, 0) if event.shift else (0, 2)))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.action_pan(*((-4, 0) if event.shift else (0, -2)))

    # ---- packets -------------------------------------------------------
    def send_packet(self, cells: list[tuple[int, int]], color: str,
                    on_done: Callable[[], None] | None = None) -> None:
        if not self.busy:
            self._packet_state = self._state()
        self.packets.append(Packet(cells, color, on_done))
        self.animate()

    def heat(self, links) -> None:
        self.hot_links.update(links)

    def cool(self, links) -> None:
        self.hot_links.subtract(links)
        self.hot_links += Counter()   # drop zero counts
        self.refresh()

    def flash_device(self, dev_id: str, color: str, ticks: int = FLASH_FRAMES) -> None:
        self.flash[dev_id] = [color, ticks]
        self.animate()

    def animate(self) -> None:
        """Make sure the animation clock is running (it stops itself when idle)."""
        self._schedule_work()
        if self._timer is None:
            self._timer = self.set_interval(1 / ANIMATION_FPS, self._tick)

    def _schedule_work(self) -> None:
        if (self._preparing or self._callbacks) and not self._work_scheduled and not self.app.paused:  # type: ignore[attr-defined]
            self._work_scheduled = True
            # A positive timer delay yields the event loop even when Textual's
            # message queue is continually refilled by this batch.
            self.set_timer(0.001, self._work)

    def _work(self) -> None:
        self._work_scheduled = False
        if not self.app.paused:  # type: ignore[attr-defined]
            self._drain_work()

    def _drain_work(self) -> None:
        """Run one bounded slice, then let input, drawing and timers run."""
        deadline = time.perf_counter() + WORK_SECONDS
        preparing = self.preparing
        while (self._preparing or self._callbacks) and time.perf_counter() < deadline:
            if self._preparing:
                steps = self._preparing[0]
                try:
                    next(steps)
                except StopIteration:
                    if self._preparing and self._preparing[0] is steps:
                        self._preparing.popleft()
            else:
                self._callbacks.popleft()()
        if preparing and not self.preparing:
            self.app.refresh_sidebar()  # type: ignore[attr-defined]
        self._schedule_work()

    def _tick(self) -> None:
        if self.app.paused:  # type: ignore[attr-defined]
            return
        if self._preparing:
            # Hold playback while building the batch: every initial request
            # still starts at pos=0 together, even across several commands.
            self._drain_work()
            return
        packets = list(self.packets)
        longest = max((len(pk.cells) + TRAIL for pk in packets), default=0)
        step = max(EFFECT_FPS / ANIMATION_FPS, (longest + 1) / PACKET_FRAMES) * self.app.animation_speed  # type: ignore[attr-defined]
        # A common pace keeps multicast copies on the same cells until they
        # branch. Scale by path length so a large layout doesn't mean a long wait.
        self.packets = []
        for pk in packets:
            pk.pos += step
            if pk.pos - TRAIL >= len(pk.cells):
                if pk.on_done:
                    self._callbacks.append(pk.on_done)
            else:
                self.packets.append(pk)
        self._drain_work()
        for dev_id in list(self.flash):
            self.flash[dev_id][1] -= 1
            if self.flash[dev_id][1] <= 0:
                del self.flash[dev_id]
        self.frame += 1
        if not self.busy and not self.flash and not self.net.storming and self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.refresh()

    # ---- rendering -----------------------------------------------------
    def _draw_order(self) -> list[Device]:
        return sorted(self.net.devices.values(), key=lambda d: d.id == self.selected)

    def refresh(self, *args, **kwargs):
        self._grid = None                        # every redraw request invalidates the frame
        return super().refresh(*args, **kwargs)

    def frame_grid(self) -> tuple[list[list[str]], list[list[str]]]:
        """The current frame's characters and styles, drawn once per refresh."""
        key = (self.size, self.pan)
        if self._grid is None or self._grid[0] != key:
            with self.net.remembering():
                self._grid = (key, *self._draw())
        return self._grid[1], self._grid[2]

    def render_line(self, y: int) -> Strip:
        """One screen row. Textual asks row by row; unchanged rows reuse their Strip."""
        chars, styles = self.frame_grid()
        base = self.rich_style                   # the widget's CSS colours, under every cell
        if base != self._base:
            self._base = base
            self._styles.clear()
            self._strips.clear()
        if y >= len(chars):
            return Strip.blank(self.size.width, base)
        row, srow = chars[y], styles[y]
        runs, start = [], 0
        for col in range(1, len(row) + 1):
            if col == len(row) or srow[col] != srow[start]:
                runs.append(("".join(row[start:col]), srow[start]))
                start = col
        key = tuple(runs)
        strip = self._strips.get(key)
        if strip is None:
            if len(self._strips) > 8 * max(1, len(chars)):
                self._strips.clear()
            strip = self._strips[key] = Strip(
                [Segment(text, self._style(st)) for text, st in runs], len(row))
        return strip

    def _style(self, st: str) -> Style:
        style = self._styles.get(st)
        if style is None:
            style = self._styles[st] = self._base + Style.parse(st) if st else self._base
        return style

    def _geometry(self) -> tuple:
        """Where every cable runs, the glyph mask of each cell and every group frame: rebuilt
        only when the layout, the cabling, the groups or what's blocked changes, not every
        animation frame."""
        net = self.net
        key = (tuple((d.id, d.x, d.y, d.width, d.nports) for d in net.devices.values()),
               tuple((l.a, l.pa, l.b, l.pb, l.role, l.up) for l in net.links),
               frozenset(net.stp_blocked),
               tuple((g, tuple(ms)) for g, ms in net.groups.items()))
        if self._geom is not None and self._geom[0] == key:
            return self._geom[1]
        paths: list[list[tuple[int, int]]] = []
        masks: dict[tuple[int, int], int] = {}
        kinds: dict[tuple[int, int], set[str]] = {}
        sides: dict[tuple[str, int], str] = {}
        broken: list[tuple[int, int]] = []
        for i, l in enumerate(net.links):
            pts = route(net, l, i)
            paths.append(pts)
            blocked = (l.a, l.pa) in net.stp_blocked or (l.b, l.pb) in net.stp_blocked
            kind = "control" if l.control else "blocked" if blocked else l.role
            for pt in pts:
                kinds.setdefault(pt, set()).add(kind)
            if not l.up:
                broken.append(pts[len(pts) // 2])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                masks[(x1, y1)] = masks.get((x1, y1), 0) | dirbit(x2 - x1, y2 - y1)
                masks[(x2, y2)] = masks.get((x2, y2), 0) | dirbit(x1 - x2, y1 - y2)
            for dev_id, port, pt in ((l.a, l.pa, pts[0]), (l.b, l.pb, pts[-1])):
                below = pt[1] > net.devices[dev_id].y
                masks[pt] = masks.get(pt, 0) | (N if below else S)
                sides[(dev_id, port)] = "bottom" if below else "top"
        bounds = {g: net.group_bounds(g) for g in net.groups}
        frame_order = sorted(net.groups, key=lambda g: len(net.chain(g)))   # outer frames first
        chains = {d: net.chain(d) for d in net.devices}
        geom = (paths, masks, kinds, sides, broken, bounds, frame_order, chains)
        self._geom = (key, geom)
        return geom

    def cable_paths(self) -> list[list[tuple[int, int]]]:
        """Reuse rendered cable routes when constructing packet paths."""
        return self._geometry()[0]

    def _draw(self) -> tuple[list[list[str]], list[list[str]]]:
        w, h = self.size.width, self.size.height
        ox, oy = self.pan
        chars = [[" "] * w for _ in range(h)]
        styles = [[""] * w for _ in range(h)]

        def put(x: int, y: int, ch: str, st: str) -> None:
            sx, sy = x - ox, y - oy
            if 0 <= sx < w and 0 <= sy < h:
                chars[sy][sx] = ch
                styles[sy][sx] = st

        net = self.net
        paths, masks, kinds, sides, broken, bounds, frame_order, chains = self._geometry()
        cell_style: dict[tuple[int, int], str] = {}
        hot = set(self.hot_links)
        if self.selected:
            hot |= {i for i, l in enumerate(net.links) if l.touches(self.selected)}
        if self.selected_group:
            members = set(net.devices_in(self.selected_group))
            hot |= {i for i, l in enumerate(net.links) if l.a in members or l.b in members}

        crawl: list[list[tuple[int, int]]] = []          # storm loops: dots run along them
        looping = net.loop_links() if net.storming else set()
        traffic = getattr(self.app, "traffic", None)   # random traffic shades cables by load
        for i, l in enumerate(net.links):
            pts = paths[i]
            alive = l.up and net.devices[l.a].up and net.devices[l.b].up
            blocked = (l.a, l.pa) in net.stp_blocked or (l.b, l.pb) in net.stp_blocked
            storm = l.a in net.storming or l.b in net.storming
            if storm and i in looping:
                crawl.append(pts)
            heat = traffic.level(l) if traffic is not None else 0.0
            st = ("red" if not alive or storm else "#585858" if blocked
                  else "bold yellow" if i in hot
                  else HEAT[0] if heat > 0.66 else HEAT[1] if heat > 0.33 else HEAT[2] if heat > 0.1
                  else LAG_COLOR if net.bundle(l) else PEER_COLOR if net.is_peer_link(l)
                  else ROLE_COLOR[l.role])
            if i in hot:
                cell_style.update(dict.fromkeys(pts, st))
            else:
                for pt in pts:
                    cell_style.setdefault(pt, st)
        frames = []
        for g in frame_order:
            sel = g == self.selected_group
            label = f" {g} "
            st = group_color(g)
            if g in net.mlag_domains:
                label = f" {g} · MLAG "
            if g == net.internet:
                label, st = f" ☁ {g} · internet ", "magenta"
            if g in net.clusters and not self.app.graph:  # type: ignore[attr-defined]
                ha = net.ha_state(g)
                label = f" {g} · HA {ha['level']} " if ha["level"] != "ok" else f" {g} · HA "
                if ha["level"] != "ok":
                    st = HA_COLOR[ha["level"]]
            st = "bold yellow" if sel else st
            x0, y0, x1, y1 = bounds[g]
            frames.append((g, st, x0, y0, x1, label))
            hz, vt = ("━", "┃") if sel else ("┄", "┆")
            for x in range(x0 + 1, x1):
                put(x, y0, hz, st)
                put(x, y1, hz, st)
            for y in range(y0 + 1, y1):
                put(x0, y, vt, st)
                put(x1, y, vt, st)
            for x, y, ch in ((x0, y0, "╭"), (x1, y0, "╮"), (x0, y1, "╰"), (x1, y1, "╯")):
                put(x, y, ch, st)

        for (x, y), m in masks.items():
            k = kinds.get((x, y), {"data"})
            table = (LINE2 if k == {"control"} else LINE_HEAVY if k == {"fabric"}
                     else LINE_DASH if k == {"blocked"} else LINE)
            put(x, y, table[m], cell_style[(x, y)])
        for x, y in broken:
            put(x, y, "✕", "bold red")
        for pts in crawl:
            for k, (x, y) in enumerate(pts):
                if (k - (self.frame * EFFECT_FPS / ANIMATION_FPS) // 2) % 4 == 0:
                    put(x, y, "•", "bold bright_red")

        # Group labels sit on the top edge, in the first gap no cable passes through.
        for g, st, x0, y0, x1, label in frames:
            starts = range(x0 + 2, max(x0 + 3, x1 - len(label)))
            lx = next((sx for sx in starts
                       if not any((sx + k, y0) in masks for k in range(len(label)))), x0 + 2)
            for k, ch in enumerate(label):
                if (lx + k, y0) not in masks:
                    put(lx + k, y0, ch, f"bold reverse {st}" if 0 < k < len(label) - 1 else st)

        for d in self._draw_order():
            self._draw_device(d, put, sides, d.id == self.selected, self.flash.get(d.id))

        if self.show_addr:
            # Labels wider than their box drop to a lower line instead of colliding.
            ends: dict[tuple[int, int], int] = {}
            for d in sorted(net.devices.values(), key=lambda d: d.x):
                text, st, y = self._addr_label(d, bounds, chains[d.id])
                if not text:
                    continue
                step = 1 if d.kind == "c" else -1

                def blocked(lv: int, dx: int) -> bool:
                    return (ends.get((y, lv), -10**6) >= d.x + dx
                            or any((d.x + dx + k, y + lv * step) in masks for k in range(len(text))))

                level, dx = next(((lv, dx) for dx in range(d.width) for lv in range(3)
                                  if not blocked(lv, dx)), (0, 0))
                ends[(y, level)] = d.x + dx + len(text)
                y += level * step
                for k, ch in enumerate(text):
                    if (d.x + dx + k, y) not in masks:
                        put(d.x + dx + k, y, ch, st)

        for pk in self.packets:
            for k in range(TRAIL):
                idx = int(pk.pos) - k
                if 0 <= idx < len(pk.cells):
                    x, y = pk.cells[idx]
                    put(x, y, "●" if k == 0 else "•", f"bold {pk.color}" if k == 0 else pk.color)

        return chars, styles

    def _addr_label(self, d: Device, bounds: dict, chain: list[str]) -> tuple[str, str, int]:
        """Short address caption drawn under computers and above gateways."""
        net = self.net
        if d.kind == "g":
            if d.id in net.public():              # its frame already says internet
                return "", "", 0
            if net.wan_ports(d.id):               # its uplink is drawn: say what it does instead
                label = ("hard NAT" if d.id in net.nat_hard else "NAT") if net.natting(d.id) else "no NAT"
                return label, "magenta", d.y - 1
            return "☁ internet", "magenta", d.y - 1
        if d.kind != "c" or self.app.graph:  # type: ignore[attr-defined]
            return "", "", 0
        y = d.y + BOX_H
        for g in chain:                           # step below any frame edges under the box
            bottom = bounds[g][3]
            if y <= bottom <= d.y + BOX_H + 3:
                y = bottom + 1
        addrs = net.addressed(d.id)
        if not addrs:
            cabled = any(net.link_index(d.id, p) is not None for p in range(1, d.nports + 1))
            return ("no addr", "red", y) if cabled else ("", "", 0)
        _, ifc = addrs[0]
        st = {"adhoc": "yellow", "static": "cyan"}.get(ifc.via, "#b2b2b2")
        return fmt(ifc.addr) + (f"+{len(addrs) - 1}" if len(addrs) > 1 else ""), st, y

    def _draw_device(self, d: Device, put, sides: dict, selected: bool, flash=None) -> None:
        if selected:
            tl, tr, bl, br, hz, vt, dn, up = "╔╗╚╝═║╤╧"
            bst = "bold yellow"
        else:
            tl, tr, bl, br, hz, vt, dn, up = "┌┐└┘─│┬┴"
            bst = d.color
        if not d.up:
            bst = "red"
        elif d.id in self.net.storming:        # pulse while drowning in a storm
            bst = "bold bright_red" if ((self.frame * EFFECT_FPS / ANIMATION_FPS) // 12) % 2 else "red"
        if flash and (flash[1] * EFFECT_FPS / ANIMATION_FPS) % 10 >= 3:      # blink on arrival
            tl, tr, bl, br, hz, vt, dn, up = "┏┓┗┛━┃┳┻"
            bst = f"bold {flash[0]}"
        x, y, w = d.x, d.y, d.width
        rows = [tl + hz * (w - 2) + tr, None, None, bl + hz * (w - 2) + br]
        for i, ch in enumerate(rows[0]):
            put(x + i, y, ch, bst)
        for i, ch in enumerate(rows[3]):
            put(x + i, y + 3, ch, bst)
        for r in (1, 2):
            put(x, y + r, vt, bst)
            put(x + w - 1, y + r, vt, bst)
            for i in range(1, w - 1):
                put(x + i, y + r, " ", "")
        label = (d.id if d.up else d.id + "✕").center(w - 2)
        lst = f"bold reverse {d.color}" if selected else f"bold {d.color}"
        if not d.up:
            lst = "bold red strike"
        lpad = len(label) - len(label.lstrip())
        for i, ch in enumerate(label):
            put(x + 1 + i, y + 1, ch, lst if lpad - 1 <= i <= lpad + len(d.id) else "")
        for p in range(1, d.nports + 1):
            px = d.port_x(p)
            side = sides.get((d.id, p))
            if side and (d.id, p) in self.net.stp_blocked:
                put(px, y + 2, "⊘", "bold yellow")        # held blocking by STP
                put(px, y + 3 if side == "bottom" else y, dn if side == "bottom" else up, bst)
            elif side:
                put(px, y + 2, "●", f"bold {d.color}")
                put(px, y + 3 if side == "bottom" else y, dn if side == "bottom" else up, bst)
            else:
                put(px, y + 2, "○", "#6c6c6c")
