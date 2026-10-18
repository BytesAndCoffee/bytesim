"""Sending things: ping, unicast and multicast, in address or graph mode."""
from __future__ import annotations

import random
import time

from rich.markup import escape

from ...constants import ADDR, HOST_MAX, REF
from ...model import Leg, NetError, fmt
from ...model.traffic import TRAFFIC_KINDS, TrafficGenerator, drop_reason
from ..cables import route
from .base import CommandHost
from .registry import command


class TrafficCommands(CommandHost):
    INTERNET = ("internet", "inet", "wan")

    @staticmethod
    def _flatten(legs: list[Leg]) -> list[tuple[str, int]]:
        path: list[tuple[str, int]] = []
        for _, _, p in legs:
            path.extend(p if not path else p[1:])
        return path

    @staticmethod
    def _reverse(path: list[tuple[str, int]]) -> list[tuple[str, int]]:
        return [(path[-1][0], -1)] + [(path[i][0], path[i + 1][1]) for i in range(len(path) - 2, -1, -1)]

    def _ifname(self, ifc: tuple[str, int]) -> str:
        return f"{ifc[0]}:{self.net.devices[ifc[0]].port_name(ifc[1])}"

    def _parse_addr(self, tok: str) -> tuple[int, int] | None:
        m = ADDR.match(tok)
        if not m:
            return None
        net, host = int(m[1]), int(m[2])
        if net < 1 or not 1 <= host < HOST_MAX:
            raise NetError(f"bad address {tok}: network ≥ 1, host 1..{HOST_MAX - 1}")
        return net, host

    def _resolve_dst(self, tok: str) -> tuple[str, list[tuple[str, int]] | None]:
        """A destination token -> (label, candidate interfaces); None means the internet."""
        net = self.net
        if tok.lower() in self.INTERNET:
            return "internet", None
        if (addr := self._parse_addr(tok)) is not None:
            hit = net.resolve(addr)
            if hit is None:
                raise NetError(f"nobody has {fmt(addr)} (ARP got no reply)")
            return f"{fmt(addr)} {hit[0]}", [hit]
        did, port = self._ref(tok)
        dev = net.get(did)
        if dev.kind == "s":
            raise NetError(f"{did} is a switch (layer 2): it has no address to reach")
        if port:
            if not net.iface(did, port).addr:
                raise NetError(f"{did}:{dev.port_name(port)} has no address")
            return f"{did}:{dev.port_name(port)}", [(did, port)]
        cands = [(did, p) for p, _ in net.addressed(did)]
        if not cands:
            raise NetError(f"{did} has no address (try: dhcp {did})")
        return did, cands

    def _best_route(self, src: str, cands: list[tuple[str, int]] | None) -> list[Leg]:
        if cands is None:
            return self.net.route(src, None)
        best, err = None, None
        for c in cands:
            try:
                legs = self.net.route(src, c)
            except NetError as e:
                err = e
                continue
            if best is None or len(self._flatten(legs)) < len(self._flatten(best)):
                best, self._last_dst = legs, c
        if best is None:
            raise err  # type: ignore[misc]
        return best

    def _src(self, tok: str) -> str:
        did = self.net.get(self._ref(tok)[0]).id
        if self.net.devices[did].kind == "s" and not self.graph:
            raise NetError(f"{did} is a switch (layer 2): it can't originate addressed traffic")
        return did

    def _plan(self, src: str, tok: str) -> tuple[str, list[tuple[str, int]], list[Leg] | None, bool]:
        """Work out how a packet gets from src to tok: (label, device path, legs, to-internet).

        In graph mode it's the shortest cable path; in address mode it follows
        networks and gateways, and legs records each layer-2 hop."""
        net = self.net
        internet = tok.lower() in self.INTERNET
        if self.graph:
            if ADDR.match(tok):
                raise NetError("graph mode ignores addresses: use device ids (or `mode addr`)")
            tree = net.tree(src)
            if internet:
                dst = next((d for d in tree if net.devices[d].kind == "g"), None)
                if dst is None:
                    raise NetError(f"no gateway reachable from {src}")
                label = "internet"
            else:
                dst = net.get(self._ref(tok)[0]).id
                label = dst
            path = net.path_in(tree, dst)
            if path is None:
                raise NetError(f"{dst} is not reachable from {src}"
                               + self.storm_hint(src, dst))  # type: ignore[attr-defined]
            return label, path, None, internet
        net.notes.clear()
        label, cands = self._resolve_dst(tok)
        try:
            legs = self._best_route(src, cands)
        except NetError as e:
            hint = self.storm_hint(src, *(c[0] for c in cands or []))  # type: ignore[attr-defined]
            raise NetError(f"{e}{hint}") from None
        # Each hop starts with an ARP request: a broadcast. Into a live loop, that's a storm.
        for a, b, _ in legs:
            fl = net.flood(a)
            if fl.storm:
                self.set_off_storm(fl, f"the ARP for {fmt(net.addr_of(b))} from {self._ifname(a)}")  # type: ignore[attr-defined]
                raise NetError(f"{self._ifname(a)} never got an ARP reply: its broadcast set off a storm")
        return label, self._flatten(legs), legs, internet

    def _learn(self, legs: list[Leg] | None, both_ways: bool = False) -> None:
        for a, b, p in legs or []:
            self.net.learn(a, p)
            if both_ways:
                self.net.learn(b, self._reverse(p))

    def _log_route(self, kind: str, color: str, src: str, label: str,
                   path: list[tuple[str, int]], legs: list[Leg] | None, internet: bool) -> None:
        net = self.net
        chain = " → ".join(f"[{net.devices[d].color}]{d}[/]" for d, _ in path)
        if internet:
            chain += " → [magenta]☁[/]"
        if legs is None:
            self.log_msg(f"[b {color}]{kind}[/] {src} → {label}: {chain}")
            return
        frm = fmt(net.addr_of(legs[0][0]))
        to = "internet" if internet else label.split()[0] if label.startswith("(") else fmt(net.addr_of(self._last_dst))
        self.log_msg(f"[b {color}]{kind}[/] {frm} → {to} [dim]({src} → {label})[/]: {chain}")
        for a, b, _ in legs:
            self.log_msg(f"  [dim]ARP {fmt(net.addr_of(b))} is-at {net.devices[b[0]].mac(b[1])} "
                         f"(asked by {self._ifname(a)})[/]")
        for note in dict.fromkeys(net.notes):
            self.log_msg(f"  [{'red' if note.startswith('⚠') else 'orchid'}]{escape(note)}[/]")

    def _routers_in(self, path: list[tuple[str, int]], legs: list[Leg] | None) -> int:
        if legs is not None:
            return len(legs) - 1
        return sum(1 for d, _ in path[1:-1] if self.net.is_router(d))

    @command("ping", "trace", "tracert", "traceroute")
    def cmd_ping(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) != 2:
            raise NetError("usage: ping <src> <id|(net,host)|internet>")
        src = self._src(args[0])
        if args[1].lower() not in self.INTERNET and self._ref_or_none(args[1]) == src:
            self.log_msg(f"{src}: reply from self (loopback)")
            return
        try:
            label, path, legs, internet = self._plan(src, args[1])
        except NetError as e:
            raise NetError(f"ping {src} → {args[1]}: {e}") from None
        if len(path) < 2:
            self.log_msg(f"[green]{src} is a gateway: internet reachable directly[/]")
            return
        self._log_route("PING", "red", src, label, path, legs, internet)
        self._learn(legs, both_ways=True)
        back = self._reverse(path)
        c = self.canvas
        links = [li for _, li in path[1:]]
        c.heat(links)
        ttl = 64 - self._routers_in(path, legs)
        rtt = sum(random.uniform(0.15, 0.6) for _ in path) * 2 + (12 if internet else 0)

        def arrived() -> None:
            c.flash_device(path[-1][0], "red")
            c.send_packet(self._cells(back), "green", replied)

        def replied() -> None:
            c.cool(links)
            c.flash_device(src, "green")
            self.log_msg(f"  [green]reply from {label}[/]: hops={len(path) - 1} "
                         f"ttl={ttl} time={rtt:.2f} ms")
            self.refresh_sidebar()

        c.send_packet(self._cells(path), "red", arrived)

    def _ref_or_none(self, tok: str) -> str | None:
        m = REF.match(tok)
        return f"{m[1].lower()}{int(m[2])}" if m else None

    def _cells(self, path: list[tuple[str, int]]) -> list[tuple[int, int]]:
        """Screen cells a packet crosses along a path, port to port."""
        net = self.net
        cells: list[tuple[int, int]] = []
        for (u, _), (v, li) in zip(path, path[1:]):
            l = net.links[li]
            pts = route(net, l, li)
            if l.a != u:
                pts = pts[::-1]
            du, dv = net.devices[u], net.devices[v]
            cells.append((du.port_x(l.port_of(u)), du.y + 2))
            cells.extend(pts)
            cells.append((dv.port_x(l.port_of(v)), dv.y + 2))
        return cells

    @command("unicast", "ucast", "send")
    def cmd_unicast(self, args: list[str], opts: dict[str, str]) -> None:
        """One packet, one destination, no reply."""
        if len(args) != 2:
            raise NetError("usage: unicast <src> <id|(net,host)|internet>")
        src = self._src(args[0])
        if self._ref_or_none(args[1]) == src:
            raise NetError("source and destination are the same device")
        try:
            label, path, legs, internet = self._plan(src, args[1])
        except NetError as e:
            raise NetError(f"unicast {src} → {args[1]}: {e}") from None
        if len(path) < 2:
            raise NetError(f"{src} is a gateway: nothing to send through")
        self._log_route("UNICAST", "orange1", src, label, path, legs, internet)
        self._learn(legs)
        c = self.canvas
        links = [li for _, li in path[1:]]
        c.heat(links)
        routers = self._routers_in(path, legs)

        def delivered() -> None:
            c.cool(links)
            c.flash_device(path[-1][0], "orange1")
            self.log_msg(f"  [orange1]delivered to {label}[/]: {len(path) - 1} hops, "
                         f"{routers} router{'s' if routers != 1 else ''}")
            self.refresh_sidebar()

        c.send_packet(self._cells(path), "orange1", delivered)

    def _targets(self, toks: list[str]) -> list[str]:
        """Expand device ids, addresses and group names into a list of device ids."""
        out: list[str] = []
        for t in toks:
            if t.lower() in ("all", "*"):
                ids = [d.id for d in self.net.sorted_devices() if d.kind == "c"]
            elif t.lower() in self.net.groups:
                ids = [d for d in self.net.devices_in(t.lower())
                       if self.graph or self.net.devices[d].kind != "s"]   # switches can't receive
            elif ADDR.match(t) and not self.graph:
                ids = [self._resolve_dst(t)[1][0][0]]  # type: ignore[index]
            else:
                ids = [self.net.get(self._ref(t)[0]).id]
            out.extend(i for i in ids if i not in out)
        return out

    @command("multicast", "mcast")
    def cmd_multicast(self, args: list[str], opts: dict[str, str]) -> None:
        """One packet, copied only where the paths to the receivers split."""
        if len(args) < 2:
            raise NetError("usage: multicast <src> <id|(net,host)|group|all> [...]")
        net = self.net
        src = self._src(args[0])
        receivers = [d for d in self._targets(args[1:]) if d != src]
        if not receivers:
            raise NetError("no receivers (the source can't send to itself)")
        paths: dict[str, list[tuple[str, int]]] = {}
        lost: list[str] = []
        for d in receivers:
            try:
                _, path, legs, _ = self._plan(src, d)
            except NetError as e:
                lost.append(f"{d} ({e})")
                continue
            self._learn(legs)
            paths[d] = path
        recv_txt = " ".join(f"[{net.devices[d].color}]{d}[/]" for d in receivers)
        frm = src if self.graph else f"{fmt(next((i.addr for _, i in net.addressed(src)), None))} [dim]({src})[/]"
        self.log_msg(f"[b bright_magenta]MULTICAST[/] {frm} → {recv_txt}")
        for msg in lost:
            self.log_msg(f"  [red]✗ unreachable: {escape(msg)}[/]")
        if not paths:
            return

        # Links in the distribution tree, and where copies get made.
        tree_links = {li for p in paths.values() for _, li in p[1:]}
        children: dict[str, set[str]] = {}
        for p in paths.values():
            for (u, _), (v, _) in zip(p, p[1:]):
                children.setdefault(u, set()).add(v)
        forks = [f"{u}×{len(vs)}" for u, vs in children.items() if len(vs) > 1]
        unicast_cost = sum(len(p) - 1 for p in paths.values())
        self.log_msg(f"  copies made at: {' '.join(forks) or 'none (single path)'}")
        self.log_msg(f"  {len(tree_links)} link transmissions "
                     f"(separate unicasts would need {unicast_cost})")

        # All copies leave together and share cells until their paths split,
        # so on screen one packet visibly forks at each branching device.
        c = self.canvas
        c.heat(tree_links)
        pending = [len(paths)]

        def arrived(dst: str) -> None:
            c.flash_device(dst, "bright_magenta")
            pending[0] -= 1
            if pending[0] == 0:
                c.cool(tree_links)
                self.log_msg(f"  [bright_magenta]delivered to {len(paths)} receiver{'s' if len(paths) != 1 else ''}[/]: "
                             + " ".join(paths))
                self.refresh_sidebar()

        for dst, p in paths.items():
            c.send_packet(self._cells(p), "bright_magenta", lambda d=dst: arrived(d))

    # ---- random background traffic --------------------------------------------
    TRAFFIC_COLORS = {"unicast": "sky_blue1", "internet": "plum2", "broadcast": "khaki1"}
    traffic: TrafficGenerator | None = None

    @command("traffic")
    def cmd_traffic(self, args: list[str], opts: dict[str, str]) -> None:
        usage = ("usage: traffic on [packets/s] [seed=N] [hosts=<group>] [mix=unicast,internet,broadcast]"
                 "  ·  traffic off  ·  traffic rate <n>  ·  traffic reset  ·  traffic")
        word = args[0].lower() if args else ""
        if word == "":
            self._traffic_report()
        elif word == "on":
            rate = self._rate(args[1]) if len(args) > 1 else 2.0
            hosts = None
            if "hosts" in opts:
                g = opts["hosts"]
                if g not in self.net.groups:
                    raise NetError(f"hosts=: no group {g}")
                hosts = {d for d in self.net.devices_in(g) if self.net.devices[d].kind == "c"}
                if not hosts:
                    raise NetError(f"{g} has no computers to send traffic")
            mix = None
            if "mix" in opts:
                kinds = [k for k in opts["mix"].split(",") if k]
                bad = [k for k in kinds if k not in TRAFFIC_KINDS]
                if bad or not kinds:
                    raise NetError(f"mix=: choose from {', '.join(TRAFFIC_KINDS)}")
                mix = {k: 1.0 for k in kinds}
            seed = int(opts["seed"]) if "seed" in opts and opts["seed"].isdigit() else None
            self.traffic = TrafficGenerator(rate, seed, hosts, mix)
            self._traffic_clock = time.monotonic()
            timer = getattr(self, "_traffic_timer", None)
            if timer is None and hasattr(self, "set_interval"):
                self._traffic_timer = self.set_interval(0.1, self._traffic_tick)  # type: ignore[attr-defined]
            who = f"computers in {opts['hosts']}" if hosts else "every computer"
            self.log_msg(f"[green]traffic on[/]: {rate:g} packets/s from {who}"
                         + (f", seed {seed}" if seed is not None else "")
                         + (f", {'+'.join(mix)} only" if mix else "")
                         + "  [dim](traffic off to stop; traffic for statistics)[/]")
        elif word == "off":
            if self.traffic is None:
                raise NetError("traffic is already off")
            timer = getattr(self, "_traffic_timer", None)
            if timer is not None:
                timer.stop()
                self._traffic_timer = None
            self._traffic_report(final=True)
            self.traffic = None
            self.refresh_sidebar()
        elif word == "rate" and len(args) == 2:
            if self.traffic is None:
                raise NetError("traffic is off: traffic on <rate>")
            self.traffic.rate = self._rate(args[1])
            self.log_msg(f"traffic rate: {self.traffic.rate:g} packets/s")
        elif word == "reset":
            if self.traffic is None:
                raise NetError("traffic is off")
            from ...model.traffic import TrafficStats
            self.traffic.stats = TrafficStats()
            self.log_msg("traffic statistics cleared")
        else:
            raise NetError(usage)

    @staticmethod
    def _rate(tok: str) -> float:
        try:
            rate = float(tok)
        except ValueError:
            raise NetError("the rate is packets per second, e.g. traffic on 5") from None
        if not 0 < rate <= 50:
            raise NetError("rate must be more than 0 and at most 50 packets/s")
        return rate

    def _traffic_tick(self) -> None:
        now = time.monotonic()
        dt, self._traffic_clock = now - self._traffic_clock, now
        self.traffic_step(dt)

    def traffic_step(self, dt: float) -> int:
        """Advance the traffic by dt seconds; returns how many packets were sent."""
        gen = self.traffic
        if gen is None or (self.replaying and not self.headless):   # paused while the app runs a script
            return 0
        gen.cool(dt)
        n = gen.due(dt)
        for _ in range(n):
            self._send_random(gen)
        if n:
            self.refresh_sidebar()
        self.canvas.refresh()
        return n

    def _send_random(self, gen: TrafficGenerator) -> None:
        flow = gen.pick(self.net)
        if flow is None:
            return
        kind, src, dst = flow
        net, c = self.net, self.canvas
        color = self.TRAFFIC_COLORS[kind]
        if kind == "broadcast":
            nic = next((src, p) for p in range(1, net.devices[src].nports + 1)
                       if net.link_index(src, p) is not None)
            if net.storm_near(src):
                gen.stats.drop(src, kind, "storm")
                return
            fl = net.flood(nic)
            if not fl.generations:
                gen.stats.drop(src, kind, "unreachable")
                return
            if fl.storm:
                gen.stats.drop(src, kind, "storm")
                self.set_off_storm(fl, f"a random broadcast from {src}")  # type: ignore[attr-defined]
                return
            net.learn_flood(fl)
            gen.stats.sent += 1
            gen.stats.kinds[kind] += 1
            gen.stats.talkers[src] += 1
            for g in fl.generations:
                for li, _ in g:
                    key = id(net.links[li])
                    gen.load[key] = gen.load.get(key, 0.0) + 1.0
            self.animate_flood(fl, color, lambda: gen.stats.arrive(src), max_hops=4)  # type: ignore[attr-defined]
            return
        try:
            _, path, legs, _ = self._plan(src, dst)
        except NetError as e:
            gen.stats.drop(src, kind, drop_reason(str(e)))
            return
        if len(path) < 2:
            return
        self._learn(legs)
        gen.stats.record(net, kind, path)
        gen.carry(net, path)
        end = path[-1][0]
        c.send_packet(self._cells(path), color, lambda: gen.stats.arrive(end))

    def _traffic_report(self, final: bool = False) -> None:
        gen = self.traffic
        if gen is None:
            self.log_msg("[dim]traffic is off — traffic on [rate] starts it[/]")
            return
        st = gen.stats
        mins, secs = divmod(int(gen.elapsed), 60)
        head = "traffic stopped" if final else f"traffic [green]on[/]: {gen.rate:g} packets/s"
        self.log_msg(f"{head} · ran {mins}m {secs:02d}s" + (f" · seed {gen.seed}" if gen.seed is not None else ""))
        drops = ", ".join(f"{r} {n}" for r, n in st.dropped.most_common())
        self.log_msg(f"  sent {st.sent} · delivered {st.delivered} · in flight {max(0, st.in_flight)} · "
                     f"dropped {sum(st.dropped.values())}" + (f" ({drops})" if drops else ""))
        if st.kinds:
            self.log_msg("  mix: " + ", ".join(f"{k} {st.kinds[k]}" for k in TRAFFIC_KINDS if st.kinds[k]))

        def top(counter, n=4):
            return " · ".join(f"{escape(k)} {v}" for k, v in counter.most_common(n))

        if st.cables:
            self.log_msg(f"  busiest cables: {top(st.cables)}")
        if st.forwarded:
            self.log_msg(f"  busiest devices (forwarded): {top(st.forwarded)}")
        if st.talkers:
            self.log_msg(f"  top talkers: {top(st.talkers)}")
