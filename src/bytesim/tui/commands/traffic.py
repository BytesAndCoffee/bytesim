"""Sending things: ping, unicast and multicast, in address or graph mode."""
from __future__ import annotations

import random
import time
from collections.abc import Iterator
from math import isfinite

from rich.markup import escape

from ...constants import ADDR, HOST_MAX, REF
from ...model import Leg, NetError, fmt
from ...model.traffic import TRAFFIC_KINDS, TrafficGenerator, drop_reason
from ..cables import route
from .base import CommandHost
from .registry import command


class TrafficCommands(CommandHost):
    INTERNET = ("internet", "inet", "wan")
    _overlay_src: tuple[int, int] | None = None   # set by _plan: the inner source of a tunnelled packet
    _carried = False                              # set by _plan: the overlay carried the last plan
    SPEEDS = {"slow": 0.5, "normal": 1.0, "fast": 3.0}
    BULK_PAIRS = 128

    @command("pause")
    def cmd_pause(self, args: list[str], opts: dict[str, str]) -> None:
        if args:
            raise NetError("usage: pause")
        self.paused = True
        self.log_msg("[yellow]paused[/] · packet actions will queue; use [cyan]resume[/] to start them together")
        self.refresh_sidebar()

    def queue_action(self, tokens: list[str]) -> None:
        self.pending_actions.append(list(tokens))
        self.log_msg(f"[yellow]queued {len(self.pending_actions)}[/]: {escape(' '.join(tokens))}")
        self.refresh_sidebar()

    @command("queue")
    def cmd_queue(self, args: list[str], opts: dict[str, str]) -> None:
        if args and [a.lower() for a in args] != ["clear"]:
            raise NetError("usage: queue [clear]")
        if args:
            count = len(self.pending_actions)
            self.pending_actions.clear()
            self.log_msg(f"cleared {count} queued action{'s' if count != 1 else ''}")
            self.refresh_sidebar()
        elif self.pending_actions:
            for i, tokens in enumerate(self.pending_actions, 1):
                self.log_msg(f"  {i}. {escape(' '.join(tokens))}")
        else:
            self.log_msg("[dim]queue empty[/]")

    @command("resume", "play", "unpause")
    def cmd_resume(self, args: list[str], opts: dict[str, str]) -> None:
        if args:
            raise NetError("usage: resume")
        actions = list(self.pending_actions)
        self.pending_actions.clear()
        self.paused = False
        failed = 0
        # Dispatch the entire batch without yielding to the animation timer:
        # all new request packets begin on the next shared tick.
        for tokens in actions:
            try:
                self.dispatch(tokens)
            except NetError as e:
                failed += 1
                self.log_msg(f"[red]✗ queued {escape(' '.join(tokens))}: {escape(str(e))}[/]")
        started = len(actions) - failed
        self.log_msg(f"[green]resumed[/] · {started} queued action{'s' if started != 1 else ''} started"
                     + (f", {failed} failed" if failed else ""))
        self.canvas.animate()
        self.refresh_sidebar()

    @command("speed")
    def cmd_speed(self, args: list[str], opts: dict[str, str]) -> None:
        usage = "usage: speed [slow|normal|fast|0.1..10]"
        if len(args) > 1:
            raise NetError(usage)
        if args:
            token = args[0].lower()
            try:
                speed = self.SPEEDS[token] if token in self.SPEEDS else float(token.removesuffix("x"))
            except ValueError:
                raise NetError(usage) from None
            if not isfinite(speed) or not 0.1 <= speed <= 10:
                raise NetError(usage)
            self.animation_speed = speed
        self.log_msg(f"animation speed: [b]{self.animation_speed:g}x[/]"
                     " · slow=0.5x · normal=1x · fast=3x")

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
        if port is not None:
            if not 1 <= port <= dev.nports:
                raise NetError(f"{did} has no port {port} (it has {dev.nports})")
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

    def _plan(self, src: str, tok: str, reply: bool = False) -> tuple[str, list[tuple[str, int]], list[Leg] | None, bool]:
        """Work out how a packet gets from src to tok: (label, device path, legs, to-internet).

        In graph mode it's the shortest cable path; in address mode it follows
        networks and gateways, and legs records each layer-2 hop. A ``reply`` belongs
        to a flow the other side started, so NAT lets it back in."""
        net = self.net
        internet = tok.lower() in self.INTERNET
        if self.graph:
            if ADDR.match(tok):
                raise NetError("graph mode ignores addresses: use device ids (or `mode addr`)")
            if "@" in tok:                        # pure connectivity: the tunnel is just the cable path
                ov, member = self._overlay_dst(tok)  # type: ignore[misc]
                for d in (src, member):
                    if d not in net.overlay_members(ov):
                        raise NetError(f"{d} is not in overlay {ov}")
                if not net.acl_allows(ov, src, member):
                    raise NetError(f"overlay {ov}'s access rules don't let {src} reach {member}")
                tok = member
            tree = net.tree(src)
            if internet:
                public = net.public()
                dst = next((d for d in tree if net.devices[d].kind == "g" and (not public or d in public)), None)
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
        self._overlay_src, self._carried = None, False
        if (overlay := self._overlay_dst(tok)) is not None:
            return self._plan_tunnel(src, *overlay)
        storm = None
        with net.remembering():
            label, cands = self._resolve_dst(tok)
            for c in (cands if cands is not None else [None]) if net.overlays and not reply else []:
                if (via := net.overlay_via(src, c)) is not None:     # a subnet router or exit node
                    legs, notes = net.routed_via(src, c, *via)
                    net.notes.extend(notes)
                    self._carried = True
                    if c is not None:
                        self._last_dst = c
                    return label, self._flatten(legs), legs, c is None
            try:
                legs = self._best_route(src, cands)
            except NetError as e:
                hint = self.storm_hint(src, *(c[0] for c in cands or []))  # type: ignore[attr-defined]
                raise NetError(f"{e}{hint}") from None
            if not reply and (blocked := net.nat_block(legs)):
                raise NetError(blocked)
            net.notes.extend(net.nat_notes(legs))
            # Every hop starts with ARP. Share structural indexes with routing,
            # but leave the scope before setting off a storm (a model mutation).
            for a, b, _ in legs:
                fl = net.flood(a)
                if fl.storm:
                    storm = a, b, fl
                    break
        if storm is not None:
            a, b, fl = storm
            self.set_off_storm(fl, f"the ARP for {fmt(net.addr_of(b))} from {self._ifname(a)}")  # type: ignore[attr-defined]
            raise NetError(f"{self._ifname(a)} never got an ARP reply: its broadcast set off a storm")
        return label, self._flatten(legs), legs, internet

    def _overlay_dst(self, tok: str) -> tuple[str, str] | None:
        """(overlay, member) for c3@ol or an overlay address; None for ordinary destinations."""
        net = self.net
        if "@" in tok:
            dev, _, ov = tok.partition("@")
            if ov.lower() not in net.overlays:
                raise NetError(f"no overlay {ov or '(none named)'} (overlay <name> <devices…> makes one)")
            return ov.lower(), net.get(self._ref(dev)[0]).id
        if (addr := self._parse_addr(tok)) is not None and (hit := net.ol_resolve(addr)):
            return hit
        return None

    def _plan_tunnel(self, src: str, ov: str, dst: str) -> tuple[str, list[tuple[str, int]], list[Leg], bool]:
        """A packet to an overlay address: WireGuard between the members' real interfaces."""
        net = self.net
        with net.remembering():
            legs, notes = net.tunnel(src, dst, ov)
        net.notes.extend(notes)
        self._last_dst = legs[-1][1] if legs else (dst, 1)
        self._overlay_src, self._carried = net.ol_addr(ov, src), True
        return f"{fmt(net.ol_addr(ov, dst))} {dst}", self._flatten(legs), legs, False

    def _learn(self, legs: list[Leg] | None, both_ways: bool = False) -> None:
        for a, b, p in legs or []:
            self.net.learn(a, p)
            if both_ways:
                self.net.learn(b, self._reverse(p))

    def _log_route(self, kind: str, color: str, src: str, label: str,
                   path: list[tuple[str, int]], legs: list[Leg] | None, internet: bool) -> None:
        if not self._show_detail():
            return
        net = self.net
        chain = " → ".join(f"[{net.devices[d].color}]{d}[/]" for d, _ in path)
        if internet:
            chain += " → [magenta]☁[/]"
        if legs is None:
            self.log_msg(f"[b {color}]{kind}[/] {src} → {label}: {chain}")
            return
        frm = fmt(self._overlay_src or net.addr_of(legs[0][0]))
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

    @command("ping", "tracert", "traceroute")
    def cmd_ping(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) >= 2 and (args[0].lower() in ("all", "*") or len(args) > 2 or args[1].lower() in ("all", "*")
                               or args[1].lower() in self.net.groups or args[1].lower().split("@")[0] in ("all", "*")):
            self.cmd_mping(args, opts)
            return
        if len(args) != 2:
            raise NetError("usage: ping <src> <id|(net,host)|internet|group|all> [...]")
        src = self._src(args[0])
        if args[1].lower() not in self.INTERNET and (
                self._ref_or_none(args[1]) == src or (self._overlay_dst(args[1]) or ("", ""))[1] == src):
            self.log_msg(f"{src}: reply from self (loopback)")
            return
        try:
            label, path, legs, internet = self._plan(src, args[1])
        except NetError as e:
            if self.trace_enabled:
                self._log_path_trace_failure("REQUEST", src, args[1], e)
            raise NetError(f"ping {src} → {args[1]}: {e}") from None
        if len(path) < 2:
            self.log_msg(f"[green]{src} is a gateway: internet reachable directly[/]")
            return
        self._log_route("PING", "red", src, label, path, legs, internet)
        if self.trace_enabled:
            self._log_path_trace("REQUEST", src, label, path, legs, internet=internet)
        self._learn(legs, both_ways=True)
        back = self._reverse(path)
        c = self.canvas
        links = [li for _, li in path[1:]]
        c.heat(links)
        ttl = 64 - self._routers_in(path, legs)
        rtt = sum(random.uniform(0.15, 0.6) for _ in path) * 2 + (12 if internet else 0)

        def arrived() -> None:
            c.flash_device(path[-1][0], "red")
            if self.trace_enabled:
                back_legs = [(b, a, self._reverse(p)) for a, b, p in reversed(legs)] if legs else None
                self._log_path_trace("REPLY", label, src, back, back_legs,
                                      dst_iface=legs[0][0] if legs else None)
            c.send_packet(self._cells(back), "green", replied)

        def replied() -> None:
            c.cool(links)
            c.flash_device(src, "green")
            metrics = (f": hops={len(path) - 1} ttl={ttl} time={rtt:.2f} ms"
                       if self._show_detail() else "")
            self.log_msg(f"  [green]reply from {label}[/]{metrics}")
            self.refresh_sidebar()

        c.send_packet(self._cells(path), "red", arrived)

    def _ref_or_none(self, tok: str) -> str | None:
        m = REF.match(tok)
        return f"{m[1].lower()}{int(m[2])}" if m else None

    def _cells(self, path: list[tuple[str, int]]) -> list[tuple[int, int]]:
        """Screen cells a packet crosses along a path, port to port."""
        net = self.net
        cells: list[tuple[int, int]] = []
        cable_paths = self.canvas.cable_paths() if not self.headless else None
        for (u, _), (v, li) in zip(path, path[1:]):
            l = net.links[li]
            pts = cable_paths[li] if cable_paths is not None else route(net, l, li)
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
            if self.trace_enabled:
                self._log_path_trace_failure("UNICAST", src, args[1], e)
            raise NetError(f"unicast {src} → {args[1]}: {e}") from None
        if len(path) < 2:
            raise NetError(f"{src} is a gateway: nothing to send through")
        self._log_route("UNICAST", "#ffaf00", src, label, path, legs, internet)
        if self.trace_enabled:
            self._log_path_trace("UNICAST", src, label, path, legs, internet=internet)
        self._learn(legs)
        c = self.canvas
        links = [li for _, li in path[1:]]
        c.heat(links)
        routers = self._routers_in(path, legs)

        def delivered() -> None:
            c.cool(links)
            c.flash_device(path[-1][0], "#ffaf00")
            metrics = (f": {len(path) - 1} hops, {routers} router{'s' if routers != 1 else ''}"
                       if self._show_detail() else "")
            self.log_msg(f"  [#ffaf00]delivered to {label}[/]{metrics}")
            self.refresh_sidebar()

        c.send_packet(self._cells(path), "#ffaf00", delivered)

    def _participants(self, whole_network: bool = False) -> list[str]:
        """Default: computers. Whole network: addressed devices, or every graph node.

        Keep configured failed devices so failures are measured rather than hidden.
        """
        devices = self.net.sorted_devices()
        if not whole_network:
            return [d.id for d in devices if d.kind == "c"]
        return [d.id for d in devices if self.graph or (d.kind != "s" and self.net.addressed(d.id))]

    def _dev_of(self, target: str) -> str:
        """The device a multicast target names (c3, c3:2, c3@ol)."""
        return self._ref(target.partition("@")[0])[0]

    def _targets(self, toks: list[str], whole_network: bool = False) -> list[str]:
        """Expand targets, retaining explicit interface/address selection and overlays
        (c3@ol, all@ol for every member, or an overlay address)."""
        out: list[str] = []
        for t in toks:
            if "@" in t:
                who, _, ov = t.lower().partition("@")
                if ov not in self.net.overlays:
                    raise NetError(f"no overlay {ov or '(none named)'} (overlay <name> <devices…> makes one)")
                ids = ([f"{d}@{ov}" for d in self.net.overlay_members(ov)] if who in ("all", "*")
                       else [f"{self.net.get(self._ref(who)[0]).id}@{ov}"])
            elif (a := self._parse_addr(t)) is not None and (hit := self.net.ol_resolve(a)):
                ids = [f"{hit[1]}@{hit[0]}"]
            elif t.lower() in ("all", "*"):
                ids = self._participants(whole_network)
            elif t.lower() in self.net.groups:
                ids = [d for d in self.net.devices_in(t.lower())
                       if self.graph or self.net.devices[d].kind != "s"]   # switches can't receive
            elif ADDR.match(t) and not self.graph:
                did, port = self._resolve_dst(t)[1][0]  # type: ignore[index]
                ids = [f"{did}:{port}"]
            else:
                did, port = self._ref(t)
                self.net.get(did)
                ids = [f"{did}:{port}" if port is not None else did]
            out.extend(i for i in ids if i not in out)
        return out

    @command("multicast", "mcast")
    def cmd_multicast(self, args: list[str], opts: dict[str, str]) -> None:
        """One packet, copied only where the paths to the receivers split."""
        if args and args[0].lower() == "ping":
            self.cmd_mping(args[1:], opts)
            return
        self._multicast(args)

    def _show_detail(self, bulk: bool = False) -> bool:
        return not bulk if self.detail_enabled is None else self.detail_enabled

    def _log_path_trace(self, phase: str, src: str, target: str,
                         path: list[tuple[str, int]], legs: list[Leg] | None, *,
                         dst_iface: tuple[str, int] | None = None, internet: bool = False) -> None:
        """Describe the chosen physical cables without running another route query."""
        net = self.net
        hops = []
        for (u, _), (v, li) in zip(path, path[1:]):
            link = net.links[li]
            a, b = self._ifname((u, link.port_of(u))), self._ifname((v, link.port_of(v)))
            hops.append(f"{a} -{link.role}#{li + 1}-> {b}")
        endpoints = ""
        if legs:
            a = legs[0][0]
            if internet:
                destination = "internet"
            else:
                b = dst_iface if dst_iface is not None else self._last_dst
                destination = f"{self._ifname(b)} {fmt(net.addr_of(b))}"
            endpoints = f" · endpoints {self._ifname(a)} {fmt(net.addr_of(a))} → {destination}"
        self.log_msg(f"[b cyan]TRACE {phase}[/] {escape(src)} → {escape(target)}{endpoints}\n"
                     f"  {escape(' | '.join(hops))}")

    def _log_path_trace_failure(self, phase: str, src: str, target: str, error: NetError) -> None:
        self.log_msg(f"[b red]TRACE {phase} FAILED[/] {escape(src)} → {escape(target)}: {escape(str(error))}")

    @command("mping", "mcastping", "multicast-ping")
    def cmd_mping(self, args: list[str], opts: dict[str, str]) -> None:
        """A shared multicast echo request, with a routed reply from each receiver."""
        # A third/trailing '*' widens all selectors. Keep the two-argument
        # forms (mping c1 * / mping * *) as their existing computer shortcuts.
        whole_network = len(args) >= 3 and args[-1] == "*"
        if whole_network:
            args = args[:-1]
        if len(args) >= 2 and args[0].lower() in ("all", "*"):
            sources = self._participants(whole_network)
            if not sources:
                kind = "pingable devices" if whole_network else "computers"
                raise NetError(f"no {kind} to send multicast ping")
            targets = self._targets(args[1:], whole_network)
            if all("@" in t for t in targets):    # only members can send over an overlay
                ovs = {t.partition("@")[2] for t in targets}
                sources = [s for s in sources if any(s in self.net.overlay_members(ov) for ov in ovs)]
            sources = [src for src in sources if any(self._dev_of(dst) != src for dst in targets)]
            if not sources:
                raise NetError("no receivers (the source can't send to itself)")
            pairs = sum(self._dev_of(dst) != src for src in sources for dst in targets)
            bulk = pairs >= self.BULK_PAIRS
            self.log_msg(f"[b bright_magenta]MULTICAST PING FROM ALL[/]: {len(sources)} senders · {pairs} requests"
                         + (" · whole network" if whole_network else "")
                         + (" · summaries (use `detail on` for every reply)" if not self._show_detail(bulk) else ""))

            def batch():
                for src in sources:
                    yield from self._multicast_steps(src, [d for d in targets if self._dev_of(d) != src], True, bulk)

            self._prepare_multicast(batch(), pairs)
            return
        self._multicast(args, echo=True, whole_network=whole_network)

    def _prepare_multicast(self, steps: Iterator[None], pairs: int) -> None:
        if pairs >= self.BULK_PAIRS and not self.headless and not self.replaying:
            self.log_msg(f"[dim]preparing {pairs} requests; playback starts together when ready[/]")
            self.canvas.prepare(steps)
        else:
            for _ in steps:
                pass

    def _multicast(self, args: list[str], echo: bool = False, whole_network: bool = False) -> None:
        if len(args) < 2:
            source = "src|all" if echo else "src"
            raise NetError(f"usage: {'mping' if echo else 'multicast'} <{source}> <id|(net,host)|group|all> [...]")
        src = self._src(args[0])
        receivers = [d for d in self._targets(args[1:], whole_network) if self._dev_of(d) != src]
        if not receivers:
            raise NetError("no receivers (the source can't send to itself)")
        bulk = len(receivers) >= self.BULK_PAIRS
        self._prepare_multicast(self._multicast_steps(src, receivers, echo, bulk), len(receivers))

    def _multicast_steps(self, src: str, receivers: list[str], echo: bool, bulk: bool) -> Iterator[None]:
        """Yield between route queries and packet creation so large batches stay interactive."""
        def detailed() -> bool:
            return self._show_detail(bulk)

        net = self.net
        paths: dict[str, list[tuple[str, int]]] = {}
        reply_targets: dict[str, str] = {}
        endpoints: dict[str, tuple[tuple[str, int], tuple[str, int]]] = {}
        lost: list[str] = []
        carried: dict[str, list[Leg]] = {}           # replies to these retrace the overlay's path
        for d in receivers:
            try:
                _, path, legs, _ = self._plan(src, d)
                if self._carried and legs:
                    carried[d] = legs
            except NetError as e:
                if self.trace_enabled:
                    self._log_path_trace_failure("REQUEST", src, d, e)
                lost.append(f"{d} ({e})")
                yield
                continue
            if self.trace_enabled:
                self._log_path_trace("REQUEST", src, d, path, legs)
            self._learn(legs)
            paths[d] = path
            if legs:
                endpoints[d] = legs[0][0], self._last_dst
            # Reply to the interface that originated this receiver's request,
            # which matters when the sender has NICs on several networks.
            if echo:
                reply_targets[d] = fmt(net.addr_of(legs[0][0])) if legs else src
            yield
        recv_txt = " ".join(f"[{net.devices[self._dev_of(d)].color}]{d}[/]" for d in receivers)
        frm = src if self.graph else f"{fmt(next((i.addr for _, i in net.addressed(src)), None))} [dim]({src})[/]"
        kind = "MULTICAST PING" if echo else "MULTICAST"
        if detailed():
            self.log_msg(f"[b bright_magenta]{kind}[/] {frm} → {recv_txt}")
            for target, (a, b) in endpoints.items():
                self.log_msg(f"  [dim]request to {target}: {self._ifname(a)} {fmt(net.addr_of(a))}"
                             f" → {self._ifname(b)} {fmt(net.addr_of(b))}[/]")
        for msg in lost if detailed() else lost[:5]:
            self.log_msg(f"  [red]✗ unreachable: {escape(msg)}[/]")
        if not detailed() and len(lost) > 5:
            self.log_msg(f"  [red]{len(lost) - 5} more unreachable receivers · source {src}[/]")
        if not paths:
            if echo:
                self.log_msg(f"  [yellow]multicast ping: 0/{len(receivers)} replies, {len(lost)} failed[/] · source {src}")
            return

        # Links in the distribution tree, and where copies get made.
        tree_links = {li for p in paths.values() for _, li in p[1:]}
        children: dict[str, set[str]] = {}
        for p in paths.values():
            for (u, _), (v, _) in zip(p, p[1:]):
                children.setdefault(u, set()).add(v)
        forks = [f"{u}×{len(vs)}" for u, vs in children.items() if len(vs) > 1]
        unicast_cost = sum(len(p) - 1 for p in paths.values())
        if detailed():
            self.log_msg(f"  copies made at: {' '.join(forks) or 'none (single path)'}")
            self.log_msg(f"  {len(tree_links)} link transmissions "
                         f"(separate unicasts would need {unicast_cost})")

        # All copies leave together and share cells until their paths split,
        # so on screen one packet visibly forks at each branching device.
        c = self.canvas
        c.heat(tree_links)
        pending = [len(paths)]
        replies: list[str] = []
        reply_failures: list[str] = []

        def finished() -> None:
            pending[0] -= 1
            if pending[0] == 0:
                c.cool(tree_links)
                if echo:
                    failed = len(lost) + len(reply_failures)
                    color = "yellow" if failed else "green"
                    self.log_msg(f"  [{color}]multicast ping: {len(replies)}/{len(receivers)} replies, {failed} failed[/]"
                                 f" · source {src}")
                else:
                    names = ": " + " ".join(paths) if detailed() else ""
                    self.log_msg(f"  [bright_magenta]delivered to {len(paths)} receiver{'s' if len(paths) != 1 else ''}[/]{names}")
                self.refresh_sidebar()

        def arrived(dst: str) -> None:
            dst_id = self._dev_of(dst)
            c.flash_device(dst_id, "bright_magenta")
            if not echo:
                finished()
                return
            try:
                net.get(dst_id)
                net.get(src)
                if dst in carried:                   # back through the subnet router, exit node or tunnel
                    legs = [(b, a, self._reverse(p)) for a, b, p in reversed(carried[dst])]
                    hops = [hop for leg in legs for hop in leg[2]]
                    if down := next((d for d, _ in hops if not net.devices[d].up), None):
                        raise NetError(f"{down} went down while the request was out")
                    if any(li >= 0 and not net.usable(net.links[li]) for _, li in hops):
                        raise NetError("a cable on the way back failed while the request was out")
                    back = self._flatten(legs)
                else:
                    _, back, legs, _ = self._plan(dst_id, reply_targets[dst], reply=True)
            except NetError as e:
                if self.trace_enabled:
                    self._log_path_trace_failure("REPLY", dst, src, e)
                reply_failures.append(dst)
                if detailed() or len(reply_failures) <= 5:
                    self.log_msg(f"  [red]✗ no reply from {dst} to {src}: {escape(str(e))}[/]")
                finished()
                return
            if self.trace_enabled:
                self._log_path_trace("REPLY", dst, src, back, legs)
            self._learn(legs)
            if detailed():
                self._log_route("ECHO REPLY", "green", dst_id, src, back, legs, False)
            links = [li for _, li in back[1:]]
            c.heat(links)
            ttl = 64 - self._routers_in(back, legs)
            rtt = sum(random.uniform(0.15, 0.6) for _ in paths[dst] + back)

            def replied() -> None:
                c.cool(links)
                c.flash_device(src, "green")
                replies.append(dst)
                if detailed():
                    self.log_msg(f"  [green]reply from {dst}[/] to {src}: hops={len(back) - 1} "
                                 f"ttl={ttl} time={rtt:.2f} ms")
                finished()

            c.send_packet(self._cells(back), "green", replied)

        for dst, p in paths.items():
            c.send_packet(self._cells(p), "bright_magenta", lambda d=dst: arrived(d))
            yield

    # ---- random background traffic --------------------------------------------
    TRAFFIC_COLORS = {"unicast": "#87d7ff", "internet": "#d7afff", "broadcast": "#ffff87"}
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
        if self.paused or gen is None or (self.replaying and not self.headless):
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
