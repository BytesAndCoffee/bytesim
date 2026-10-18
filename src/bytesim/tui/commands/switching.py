"""Switching loops: broadcasts, spanning tree, and broadcast storms."""
from __future__ import annotations

from typing import Callable

from ...model import NetError
from ...model.stp import DEFAULT_PRIORITY, Flood
from ..cables import route
from .base import CommandHost
from .registry import command

ANIMATED_STORM_HOPS = 6      # how many generations of a storm to draw before it's declared


class SwitchingCommands(CommandHost):
    @command("broadcast", "bcast", "flood")
    def cmd_broadcast(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) != 1:
            raise NetError("usage: broadcast <id[:port]>   (one frame to ff:ff:ff:ff:ff:ff)")
        nic = self._sending_port(args[0])
        name = self._ifname(nic)  # type: ignore[attr-defined]
        net = self.net
        mac = net.devices[nic[0]].mac(nic[1])
        self._refuse_if_storming(nic, "broadcast")
        fl = net.flood(nic)
        if not fl.generations:
            raise NetError(f"{name} can't send: its cable is down or blocked")
        if fl.storm:
            self.log_msg(f"[b gold1]BROADCAST[/] {name} [dim]{mac}[/] → everyone")
            self.set_off_storm(fl, f"a broadcast from {name}")
            return
        net.learn_flood(fl)
        hops, reached = len(fl.generations), sorted(fl.delivered)
        self.log_msg(f"[b gold1]BROADCAST[/] {name} [dim]{mac}[/] → everyone: reached "
                     f"{len(reached)} interface{'s' if len(reached) != 1 else ''} in {hops} hop"
                     f"{'s' if hops != 1 else ''}, then the frames died out (no loop)")

        def done() -> None:
            for dev, _ in reached:
                self.canvas.flash_device(dev, "gold1")
            self.refresh_sidebar()

        self.animate_flood(fl, "gold1", done)

    @command("stp")
    def cmd_stp(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        words = [a.lower() for a in args]
        if not words:
            self._stp_status()
            return
        if words[0] == "priority":
            if len(args) != 3 or not args[2].isdigit():
                raise NetError("usage: stp priority <switch> <0-65535>  (lower wins the root election)")
            sw = self._switch(args[1])
            net.stp_priority[sw] = int(args[2])
            if net.stp_priority[sw] == DEFAULT_PRIORITY:
                del net.stp_priority[sw]
            self.log_msg(f"{sw}: bridge priority {args[2]}")
            self.changed()
            return
        if words[-1] not in ("on", "off"):
            raise NetError("usage: stp  ·  stp on|off  ·  stp <switch…> on|off  ·  stp priority <switch> <n>")
        switches = ([self._switch(a) for a in args[:-1]] if len(args) > 1
                    else [d.id for d in net.sorted_devices() if d.kind == "s"])
        if words[-1] == "on":
            net.stp_off -= set(switches)
        else:
            net.stp_off |= set(switches)
        self.log_msg(f"STP {words[-1]} for {' '.join(switches) if len(args) > 1 else 'every switch'}")
        self.changed()

    @command("lag")
    def cmd_lag(self, args: list[str], opts: dict[str, str]) -> None:
        """lag <switch> <switch>: bundle their parallel cables into one logical link."""
        net = self.net
        if len(args) != 2:
            raise NetError("usage: lag <switch> <switch>   (bundles every data cable between them)")
        a, b = (self._switch(t) for t in args)
        if a == b:
            raise NetError("a link aggregation joins two different switches")
        cables = [l for l in net.links if {l.a, l.b} == {a, b} and l.role == "data"]
        if len(cables) < 2:
            raise NetError(f"{a} and {b} have {len(cables)} data cable{'s' if len(cables) != 1 else ''} "
                           f"between them: a link aggregation needs at least two")
        pair = frozenset((a, b))
        if pair in net.lags:
            raise NetError(f"{a}–{b} is already a link aggregation")
        net.lags.add(pair)
        self.log_msg(f"[steel_blue1]≡ lag {a}–{b}[/]: {len(cables)} cables act as one link "
                     f"[dim](STP won't block them; flows are spread across them)[/]")
        self.changed()

    @command("unlag")
    def cmd_unlag(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if len(args) != 2:
            raise NetError("usage: unlag <switch> <switch>")
        pair = frozenset(self.net.get(self._ref(t)[0]).id for t in args)
        if pair not in net.lags:
            raise NetError(f"{' and '.join(sorted(pair))} aren't a link aggregation")
        net.lags.discard(pair)
        self.log_msg(f"{'–'.join(sorted(pair))}: separate cables again (STP may block all but one)")
        self.changed()

    @command("lags")
    def cmd_lags(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not net.lags:
            self.log_msg("[dim]no link aggregations: lag <switch> <switch> bundles parallel cables[/]")
        for pair in sorted(net.lags, key=lambda p: sorted(p)):
            a, b = sorted(pair, key=lambda d: (d[0], int(d[1:])))
            members = [l for l in net.links if net.bundle(l) == pair]
            up = sum(1 for l in members if net.usable(l))
            ports = " ".join(f"{a}:p{l.port_of(a)}" for l in members)
            self.log_msg(f"[steel_blue1]≡ {a}–{b}[/]: {up}/{len(members)} members carrying traffic [dim]({ports})[/]")

    # ---- storms -----------------------------------------------------------
    def set_off_storm(self, fl: Flood, cause: str) -> None:
        """A broadcast hit a live loop: mark the storm, explain it, and draw it."""
        net = self.net
        before = set(net.storming)
        net.start_storm(fl)
        totals = fl.totals
        growth = (" → ".join(f"{n:,}" for n in totals[:7])
                  + (f" → … → {totals[-1]:,}" if len(totals) > 7 else ""))
        switches = " ".join(sorted(net.storming - before, key=net.bridge_id))
        # A plain ring just circles; a meshier loop copies the frame faster than it dies.
        multiplying = totals[-1] > 2 * max(totals[:4])
        how = "multiplying as it goes" if multiplying else "copies circling in both directions"
        self.log_msg(f"[b red]⚡ BROADCAST STORM[/] set off by {cause}: the frame goes round a switching "
                     f"loop with no STP to block it, {how}")
        self.log_msg(f"  [red]frames in flight per hop: {growth}, and it never stops "
                     f"(layer-2 frames have no TTL)[/]")
        mac = net.devices[fl.source[0]].mac(fl.source[1])
        for sw, ports in sorted(fl.flaps.items(), key=lambda kv: net.bridge_id(kv[0])):
            self.log_msg(f"  [orange1]MAC {mac} flapping on {sw}: "
                         f"{'⇄'.join(f'p{p}' for p in sorted(ports))}[/]")
        self.log_msg(f"  [red]{switches} saturated: nothing else gets through until the loop is broken "
                     f"([b]stp on[/b], or fail/unlink a cable in it)[/]")
        self.animate_flood(fl, "red", None, max_hops=ANIMATED_STORM_HOPS)
        self.canvas.animate()
        self.changed()

    def storm_hint(self, *devs: str) -> str:
        """' (broadcast storm on …)' if a storm is what's in the way, else ''."""
        hit: set[str] = set()
        for d in devs:
            if d in self.net.devices:
                hit |= self.net.storm_near(d)
        if not hit:
            return ""
        return f" — broadcast storm on {' '.join(sorted(hit, key=self.net.bridge_id))} is saturating the links"

    def _refuse_if_storming(self, nic: tuple[str, int], what: str) -> None:
        if self.net.storm_near(nic[0]):
            raise NetError(f"{what} from {self._ifname(nic)} can't get out"  # type: ignore[attr-defined]
                           + self.storm_hint(nic[0]))

    def animate_flood(self, fl: Flood, color: str, on_done: Callable[[], None] | None,
                      max_hops: int | None = None) -> None:
        """Draw a flood generation by generation: every copy of hop n, then hop n+1…"""
        net, c = self.net, self.canvas
        gens = fl.generations[:max_hops] if max_hops else fl.generations
        links = {li for g in gens for li, _ in g}
        c.heat(links)

        def run(i: int) -> None:
            if i >= len(gens):
                c.cool(links)
                if on_done:
                    on_done()
                return
            moves = list(gens[i])
            pending = [len(moves)]

            def arrived() -> None:
                pending[0] -= 1
                if pending[0] == 0:
                    run(i + 1)

            hot = "red" if color == "red" or i >= 2 and fl.storm else color
            for li, dev in moves:
                l = net.links[li]
                frm = l.other(dev)[0]
                pts = route(net, l, li)
                if l.a != frm:
                    pts = pts[::-1]
                df, dt = net.devices[frm], net.devices[dev]
                cells = ([(df.port_x(l.port_of(frm)), df.y + 2)] + pts
                         + [(dt.port_x(l.port_of(dev)), dt.y + 2)])
                c.send_packet(cells, hot, arrived)

        run(0)

    # ---- helpers ------------------------------------------------------------
    def _switch(self, tok: str) -> str:
        did = self.net.get(self._ref(tok)[0]).id
        if self.net.devices[did].kind != "s":
            raise NetError(f"{did} isn't a switch: STP runs on switches")
        return did

    def _sending_port(self, tok: str) -> tuple[str, int]:
        did, port = self._ref(tok)
        dev = self.net.get(did)
        if port is None:
            port = next((p for p in range(1, dev.nports + 1) if self.net.link_index(did, p) is not None), None)
            if port is None:
                raise NetError(f"{did} has no cables")
        elif self.net.link_index(did, port) is None:
            raise NetError(f"{did}:{dev.port_name(port)} has no cable")
        return dev.id, port

    def _stp_status(self) -> None:
        net = self.net
        areas = [a for a in net._areas(net._trunks(honor_stp=False)) if len(a) > 1]
        live = net.loop_links(honor_stp=True)
        if not areas:
            self.log_msg("[dim]no switch is cabled to another switch: nothing for STP to do[/]")
        for area in areas:
            order = sorted(area, key=net.bridge_id)
            root = net.stp_roots.get(order[0])
            off = [s for s in order if not net.stp_enabled(s)]
            blocked = sorted((s, p) for s, p in net.stp_blocked if s in area)
            loops = {i for i in live if net.links[i].a in area}
            line = f"[b]{' '.join(order)}[/]: " + (f"root bridge [cyan]{root}[/]" if root else "[yellow]no STP switch[/]")
            if off:
                line += f" · STP off on {' '.join(off)}"
            self.log_msg(line)
            if blocked:
                self.log_msg("  blocking " + " ".join(f"{s}:p{p}" for s, p in blocked))
            if area & net.storming:
                self.log_msg("  [b red]⚡ broadcast storm in progress[/]")
            elif loops:
                self.log_msg(f"  [red]live loop over {len(loops)} cables: the next broadcast here will storm[/]")
            else:
                self.log_msg("  [green]loop-free[/]")
