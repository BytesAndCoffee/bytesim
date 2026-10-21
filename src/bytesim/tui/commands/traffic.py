"""Sending things: ping, unicast and multicast, in address or graph mode."""
from __future__ import annotations

import random
import time
from collections.abc import Iterator
from math import isfinite

from rich.markup import escape

from ...constants import ADDR, HOST_MAX, NAT_IDLE, REF, TTL, VIP_HOST
from ...model import Leg, NetError, fmt
from ...model.routing import OnLinkMiss
from ...model.traffic import TRAFFIC_KINDS, TrafficGenerator, drop_reason
from ..cables import route
from .base import CommandHost
from .registry import command


def clock_text(seconds: float) -> str:
    """Simulated time as h:mm:ss."""
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


class NoArpReply(NetError):
    """An address no interface has: whoever asks for it by ARP gets no reply."""

    def __init__(self, addr: tuple[int, int]) -> None:
        super().__init__(f"nobody has {fmt(addr)} (ARP got no reply)")
        self.addr = addr


class TrafficCommands(CommandHost):
    INTERNET = ("internet", "inet", "wan")
    _overlay_src: tuple[int, int] | None = None   # set by _plan: the inner source of a tunnelled packet
    _carried = False                              # set by _plan: the overlay carried the last plan
    _relay: str | None = None                     # set by _plan: the relay the last tunnel went through
    _arp_hits: list[bool] = []                    # set by _plan: which hops' ARP the cache answered
    _arp_asked: list[tuple[int, int]] = []        # set by _plan: the address each hop asked ARP for
    _failed: tuple[str, int] | None = None         # set by _best_route: the candidate whose error it raised
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

    @command("wait", "sleep")
    def cmd_wait(self, args: list[str], opts: dict[str, str]) -> None:
        """Let simulated time pass: MAC entries, ARP caches, NAT mappings and leases age."""
        usage = "usage: wait <seconds>  (or 90s, 5m, 2h)  ·  wait  shows the clock"
        if len(args) > 1:
            raise NetError(usage)
        if args:
            tok = args[0].lower()
            scale = {"s": 1, "m": 60, "h": 3600}.get(tok[-1], 1)
            try:
                seconds = float(tok[:-1] if tok[-1] in "smh" else tok) * scale
            except ValueError:
                raise NetError(usage) from None
            if not isfinite(seconds) or not 0 < seconds <= 7 * 24 * 3600:
                raise NetError("wait between 0 and a week (604800 seconds)")
            for note in self.net.advance(seconds):
                self.log_msg(f"  [orchid]{escape(note)}[/]")
        self.log_msg(f"clock: [b]{clock_text(self.net.clock)}[/]")
        self.refresh_sidebar()

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
                raise NoArpReply(addr)
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

    def _best_route(self, src: str, cands: list[tuple[str, int]] | None, port: int | None = None) -> list[Leg]:
        if cands is None:
            return self.net.route(src, None, port)
        best, err = None, None
        for c in cands:
            try:
                legs = self.net.route(src, c, port)
            except NetError as e:
                err, self._failed = e, c          # the error raised is this candidate's
                continue
            if best is None or len(self._flatten(legs)) < len(self._flatten(best)):
                best, self._last_dst = legs, c
        if best is None:
            raise err  # type: ignore[misc]
        return best

    def _src(self, tok: str) -> tuple[str, int | None]:
        """A sender and the interface it was asked to send from (None: its choice)."""
        did, port = self._ref(tok)
        dev = self.net.get(did)
        if dev.kind == "s" and not self.graph:
            raise NetError(f"{dev.id} is a switch (layer 2): it can't originate addressed traffic")
        if port is not None and not 1 <= port <= dev.nports:
            raise NetError(f"{dev.id} has no port {port} (it has {dev.nports})")
        return dev.id, port

    def _plan(self, src: str, tok: str, reply: bool = False, port: int | None = None,
              remote: str | None = None, stream: bool = False) -> tuple[str, list[tuple[str, int]], list[Leg] | None, bool]:
        """Work out how a packet gets from src to tok: (label, device path, legs, to-internet).

        In graph mode it's the shortest cable path; in address mode it follows
        networks and gateways, and legs records each layer-2 hop. A ``reply`` belongs
        to a flow the other side started: it opens no NAT mappings, and gets back in
        through the ones its request made (from ``remote``, src unless the reply comes
        from the internet). A ``stream`` packet (unicast, like UDP) may also ride a
        flow the other side started back in; an echo request never does, since NAT
        tells requests from replies. A ``port`` sends from that interface of src."""
        net = self.net
        internet = tok.lower() in self.INTERNET
        if self.graph:
            if port is not None:
                raise NetError("graph mode follows cables, not interfaces: send from the device, "
                               "without :port (or `mode addr`)")
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
        self._overlay_src, self._carried, self._relay = None, False, None
        if (overlay := self._overlay_dst(tok)) is not None:
            if port is not None:
                raise NetError("an overlay packet leaves by its tunnel: send from the device, without :port")
            label, _, legs, _ = self._plan_tunnel(src, *overlay)
            done = self._hops(legs)
            return label, self._flatten(done), done, False
        unanswered = asker = no_reply = None
        proto = "udp" if stream else "icmp"
        nat = None          # (remote, source port, protocol): the flow, for the NAT gateways it leaves by
        stop = None         # (leg, why): the packet stops at the end of that leg, and why
        net.nat_returning = {}
        with net.remembering():
            try:
                label, cands = self._resolve_dst(tok)
            except NoArpReply as e:
                unanswered = e.addr
                legs, asker = self._arp_probe(src, e.addr, port)
                label, cands = fmt(e.addr), []
                if not reply:
                    nat = fmt(e.addr), net.ephemeral(), proto
            else:
                carried = None
                for c in (cands if cands is not None else [None]) if net.overlays and not reply else []:
                    if (via := net.overlay_via(src, c)) is not None:     # a subnet router or exit node
                        if port is not None:
                            raise NetError(f"{tok} is reached through overlay {via[0]}: "
                                           f"send from the device, without :port")
                        legs, notes = net.routed_via(src, c, *via)
                        net.notes.extend(notes)
                        self._carried = True
                        if c is not None:
                            self._last_dst = c
                        carried = c
                        break
                else:
                    # The flow, whatever becomes of it: a NAT gateway the packet reaches and
                    # leaves by maps it, even if a later hop drops it.
                    flow = None if reply else (cands[0][0] if cands else "internet", net.ephemeral(), proto)
                    # Back from the internet, a packet always arrives on the WAN side and
                    # needs its mapping; from anywhere else, it needs one only where the
                    # sender has no route in (a private LAN behind NAT).
                    legs = self._nat_return(src, cands, remote, proto) if remote == "internet" else None
                    if legs is None:
                        self._failed = cands[0] if cands else None
                        try:
                            legs = self._best_route(src, cands, port)
                        except OnLinkMiss as e:
                            # Its own network (or the last router's): it asks there by ARP.
                            legs, _ = net.trace_route(src, self._failed, port)   # the route that failed
                            hint = self.storm_hint(src, *(c[0] for c in cands or []))  # type: ignore[attr-defined]
                            unanswered, asker, no_reply, nat = e.addr, e.asker, f"{e}{hint}", flow
                        except NetError as e:
                            legs = self._nat_return(src, cands, remote or src, proto) if reply or stream else None
                            if legs is None:
                                why = self._nat_explain(src, cands)
                                hint = self.storm_hint(src, *(c[0] for c in cands or []))  # type: ignore[attr-defined]
                                # It still travels as far as the router that can't forward it.
                                legs, _ = net.trace_route(src, self._failed, port)
                                stop, nat = (len(legs) - 1, why or f"{e}{hint}"), flow
                        else:
                            # A route in (a static one, say) doesn't get past NAT: crossing
                            # from WAN to LAN still takes a mapping.
                            if (blocked := self._nat_blocked_at(legs)) is not None:
                                back = self._nat_return(src, cands, remote or src, proto) if reply or stream else None
                                if back is None:
                                    stop, nat = blocked, flow
                                else:
                                    legs = back
                            else:
                                nat = flow
                if self._carried:
                    internet = carried is None
        done = self._hops(legs, unanswered, asker, nat, stop, no_reply)
        return label, self._flatten(done), done, internet

    def _nat_blocked_at(self, legs: list[Leg]) -> tuple[int, str] | None:
        """(leg, why) if a NAT gateway drops this packet: one it reaches at the end of that
        leg, coming in from its WAN side with nothing inside having asked for it."""
        for i in range(len(legs) - 1):
            if (x := self.net.nat_crossing(legs, i)) is not None and x[0] == "in":
                return i, self.net.nat_block(legs)  # type: ignore[return-value]
        return None

    def _hops(self, legs: list[Leg], unanswered: tuple[int, int] | None = None,
              asker: tuple[str, int] | None = None, nat: tuple[str, int, str] | None = None,
              stop: tuple[int, str] | None = None, no_reply: str | None = None) -> list[Leg]:
        """Send a packet hop by hop, each hop's ARP (cached, or a broadcast that may set
        off a storm) and then its frame. A router acts only once the packet has reached
        it: it maps the flow if it's a NAT gateway the packet leaves by (``nat``: the
        remote, source port and protocol), refreshes a mapping the packet is returning through, or
        drops it (``stop``: after that leg, for that reason), or the TTL runs out there.
        With ``unanswered``, the last ARP (from ``asker``) is for an address nobody has
        (``no_reply`` says why it fails, if not just that)."""
        net = self.net
        if stop is not None and stop[0] < 0:
            raise NetError(stop[1])                    # it never left the sender
        self._arp_asked = [self._next_hop_addr(legs, i) for i in range(min(len(legs), TTL))]
        self._arp_hits = []
        done: list[Leg] = []
        source = (net.addr_of(legs[0][0]), nat[1]) if nat and legs else None
        for i, (a, b, p) in enumerate(legs[:TTL]):
            addr = self._arp_asked[i]
            fl = self._ask(a, b, addr)
            self._arp_hits.append(fl is None)
            done += self._exchange([(a, b, p)], [fl], [addr])
            if stop is not None and stop[0] == i:
                raise NetError(stop[1])
            if i + 1 >= len(legs) or i + 1 >= TTL:
                continue                               # delivered, or the TTL runs out at b
            r = b[0]
            if nat and net.nat_crossing(legs, i) == ("out", r):
                source, note = net.nat_map(r, legs[i + 1][0][1], source, nat[0], nat[2])  # type: ignore[arg-type]
                net.notes.append(note)
            if (m := net.nat_returning.get(r)) is not None:
                m["expires"] = net.clock + NAT_IDLE
        if net.mlag_dual:
            crossed = {net.end(d) for _, _, p in done for d, _ in p}
            for g in sorted(net.mlag_dual & crossed):
                net.notes.append(f"⚠ crosses MLAG {g}, which is dual-active (peer link and keepalive down)")
        # Routers it passes: every leg ends at one but the last, unless a router at
        # the end still has to ask for the destination (then it's forwarding too).
        routers = len(legs) - 1 + (1 if unanswered is not None and legs else 0)
        if routers >= TTL:
            raise NetError(f"TTL expired at {legs[TTL - 1][1][0]}, the {TTL}th router "
                           f"(time exceeded: {routers} routers on the way)")
        if unanswered is not None:
            net.learn_flood(self._ask(asker, None, unanswered))  # type: ignore[arg-type]
            raise NetError(no_reply or f"nobody has {fmt(unanswered)} (ARP from {self._ifname(asker)} got no reply)")  # type: ignore[arg-type]
        return done

    def _ask(self, a: tuple[str, int], b: tuple[str, int] | None, addr: tuple[int, int]):
        """a's ARP for addr: None if its cache answers, else the broadcast (or a storm)."""
        net = self.net
        with net.remembering():
            # Share structural indexes, but leave the scope before setting off a storm.
            if b is not None and (cached := net.arp_lookup(a, addr)) is not None:
                if cached != b and addr[1] == VIP_HOST:
                    net.arp_store(a, addr, b)   # the virtual gateway's new owner announced itself
                return None
            fl = net.flood(a)
        if fl.storm:
            self.set_off_storm(fl, f"the ARP for {fmt(addr)} from {self._ifname(a)}")  # type: ignore[attr-defined]
            raise NetError(f"{self._ifname(a)} never got an ARP reply: its broadcast set off a storm")
        return fl

    def _nat_return(self, src: str, cands: list[tuple[str, int]] | None, remote: str,
                    proto: str = "udp") -> list[Leg] | None:
        """Traffic back to an inside host on a flow it started: through the NAT mappings."""
        for c in cands or []:
            if (hit := self.net.nat_return(src, remote, c, proto)) is not None:
                legs, notes = hit
                self.net.notes.extend(notes)
                self._last_dst = c
                return legs
        return None

    def _nat_explain(self, src: str, cands: list[tuple[str, int]] | None) -> str | None:
        """Why a destination behind NAT can't be reached from src, if that's the reason."""
        for c in cands or []:
            if (why := self.net.nat_drop(src, c)) is not None:
                return why
        return None

    def _arp_probe(self, src: str, addr: tuple[int, int], port: int | None) -> tuple[list[Leg], tuple[str, int]]:
        """For an address nobody has: the hops to the interface that asks for it by
        ARP (src's own, if the network is on-link, else a router's on that network)."""
        net = self.net
        own = [(src, p) for p, ifc in net.addressed(src) if ifc.net == addr[0]
               and (port is None or p == net.lag_primary(src, port))]
        if own:
            return [], own[0]
        routers = [(d, p) for key, ifc in net.ifaces.items() if ifc.net == addr[0]
                   for d, p in [(key.split(":")[0], int(key.split(":")[1]))]
                   if net.is_router(d) and d in net.devices]
        if not routers:
            raise NetError(f"nobody has {fmt(addr)}: no route to network {addr[0]}")
        legs = self._best_route(src, sorted(routers), port)
        return legs, self._last_dst

    def _next_hop_addr(self, legs: list[Leg], i: int) -> tuple[int, int]:
        """The address hop i asks ARP for: a computer handing off to its gateway asks
        for the gateway address it was configured with (a cluster's virtual one, say);
        every other hop for the interface it's sending to."""
        a, b, _ = legs[i]
        if i < len(legs) - 1 and self.net.devices[a[0]].kind == "c":
            ifc = self.net.iface(*a)
            if ifc.gw is not None:
                return ifc.net, ifc.gw
        return self.net.addr_of(b)

    def _exchange(self, legs: list[Leg], floods: list, asked: list[tuple[int, int]]) -> list[Leg]:
        """Each hop's ARP and frame, switched by the MAC tables they teach.

        The request floods and every switch it crosses learns the asker's MAC; the
        reply travels back by those entries, teaching its path the answer's MAC; the
        frame then follows the entries. Where its switches send it the way the route
        planned (bundle members aside), the route's member choices stand.""" 
        net = self.net
        out: list[Leg] = []
        for (a, b, p), fl, addr in zip(legs, floods, asked):
            if fl is None:                             # cached: straight to the MAC it remembers
                target = net.arp_lookup(a, addr)
            else:
                net.learn_flood(fl)
                reply, why = net.forward(b, a)
                if reply is None:
                    raise NetError(f"{self._ifname(a)} never got an ARP reply from {self._ifname(b)}: {why}")
                net.learn(b, reply)
                net.arp_store(a, addr, b)               # the asker learns the answer,
                net.arp_store(b, net.addr_of(a), a)     # and the target the asker
                target = b
            stale = " (a stale ARP entry: dropped, so the next packet asks again)"
            if target[0] not in net.devices:
                net.arp_evict(a, addr)
                raise NetError(f"the frame from {self._ifname(a)} for {fmt(addr)} was lost: "
                               f"nothing has its cached MAC any more{stale}")
            frame, why = net.forward(a, target)
            flooded = list(net.flooded)
            if frame is None:
                if fl is None:
                    net.arp_evict(a, addr)
                raise NetError(f"the frame from {self._ifname(a)} to {self._ifname(target)} was lost: "
                               f"{why}{stale if fl is None else ''}")
            net.learn(a, frame)                        # every switch it crossed learns its source
            if target != b:
                net.arp_evict(a, addr)
                raise NetError(f"the frame from {self._ifname(a)} for {fmt(addr)} went to {self._ifname(target)}, "
                               f"which no longer has that address{stale}")
            if flooded:
                mac = net.devices[b[0]].mac(b[1])
                # Follow the frame's own copies: known switches send it one way, only the
                # ones that don't know it flood. A storm means those copies found a loop.
                if (storm := net.flood(a, dst_mac=mac)).storm:
                    self.set_off_storm(storm, f"the unknown-unicast frame from {self._ifname(a)}")  # type: ignore[attr-defined]
                    raise NetError(f"{' '.join(flooded)} flooded the frame for {mac} (unknown unicast) "
                                   f"into a loop: it set off a storm")
                net.notes.append(f"unknown unicast: {' '.join(flooded)} flooded the frame for {mac}")
            out.append((a, b, p if self._same_way(frame, p) else frame))
        return out

    def _same_way(self, frame: list[tuple[str, int]], planned: list[tuple[str, int]]) -> bool:
        """Did the switches send the frame the way the route planned: the same devices,
        over the same cables or other members of the same bundles?"""
        net = self.net
        if [d for d, _ in frame] != [d for d, _ in planned]:
            return False
        return all(x == y or (pair := net.bundle(net.links[x])) is not None and pair == net.bundle(net.links[y])
                   for (_, x), (_, y) in zip(frame[1:], planned[1:]))

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
            conn = net.connection(src, dst, ov)
        legs, self._relay = conn.legs, conn.relay
        net.notes.extend(conn.notes)
        self._last_dst = legs[-1][1] if legs else (dst, 1)
        self._overlay_src, self._carried = net.ol_addr(ov, src), True
        return f"{fmt(net.ol_addr(ov, dst))} {dst}", self._flatten(legs), legs, False

    def _retrace_context(self, dst: str, dst_id: str, src: str, legs: list[Leg], relay: str | None) -> str:
        """Set what a reply retracing its request's path reports (its own endpoints and
        notes, not the last request planned) and return its label."""
        net = self.net
        net.notes.clear()
        self._last_dst = legs[-1][1]
        if (hit := self._overlay_dst(dst)) is None:      # through a subnet router or exit node
            self._overlay_src = None
            net.notes.append(f"a reply: back along the request's path to {src}")
            return src
        ov = hit[0]
        here, there = net.ol_addr(ov, dst_id), net.ol_addr(ov, src)
        if here is None or there is None:
            raise NetError(f"{dst_id if here is None else src} left overlay {ov} while the request was out")
        self._overlay_src = here
        net.notes.append(f"overlay {ov}: {fmt(here)} {dst_id} ⇢ {fmt(there)} {src}, "
                         f"a reply: back through the request's tunnel"
                         + (f" (relayed via {relay})" if relay else " (direct)"))
        return f"{fmt(there)} {src}"

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
        hits = self._arp_hits if len(self._arp_hits) == len(legs) else [False] * len(legs)
        asked = self._arp_asked if len(self._arp_asked) == len(legs) else [net.addr_of(b) for _, b, _ in legs]
        for (a, b, _), hit, addr in zip(legs, hits, asked):
            self.log_msg(f"  [dim]ARP {fmt(addr)} is-at {net.devices[b[0]].mac(b[1])} "
                         f"({'cached by' if hit else 'asked by'} {self._ifname(a)})[/]")
        for note in dict.fromkeys(net.notes):
            self.log_msg(f"  [{'red' if note.startswith('⚠') else 'orchid'}]{escape(note)}[/]")

    def _routers_in(self, path: list[tuple[str, int]], legs: list[Leg] | None) -> int:
        if legs is not None:
            return len(legs) - 1
        return sum(1 for d, _ in path[1:-1] if self.net.is_router(d))

    TRACE_HOPS = 30

    @command("traceroute", "tracert")
    def cmd_traceroute(self, args: list[str], opts: dict[str, str]) -> None:
        """Probes with TTL 1, 2, 3…, each forwarded on its own: the router where one
        runs out answers "time exceeded" (that answer routed back on its own), the
        destination answers the last. A router that can't forward a probe ends it."""
        if len(args) != 2:
            raise NetError("usage: traceroute <src> <id|(net,host)|internet>")
        if self.graph:
            self.log_msg("[dim]graph mode has no TTLs: tracing the cable path with a ping[/]")
            self.cmd_ping(args, opts)
            return
        src, port = self._src(args[0])
        net = self.net
        if self._overlay_dst(args[1]) is not None:
            raise NetError("traceroute follows real addresses: trace a member's real address, "
                           "or ping its overlay address")
        net.notes.clear()
        self._overlay_src, self._carried, self._relay = None, False, None
        try:
            with net.remembering():
                label, cands = self._resolve_dst(args[1])
        except NetError as e:
            raise NetError(f"traceroute {src} → {args[1]}: {e}") from None
        # Every probe of the flow takes the same way: route it once, as far as it goes.
        best: tuple[list[Leg], NetError | None, tuple[str, int] | None] | None = None
        for c in cands if cands is not None else [None]:
            legs, err = net.trace_route(src, c, port)
            if err is None or best is None or len(legs) > len(best[0]):
                best = legs, err, c
            if err is None:
                break
        legs, err, target = best  # type: ignore[misc]
        why = str(err) if err is not None else None
        if why is None and (blocked := self._nat_blocked_at(legs)) is not None:
            legs, why = legs[:blocked[0] + 1], blocked[1]
        if not legs:
            raise NetError(f"traceroute {src} → {args[1]}: {why}")
        internet = cands is None
        remote = "internet" if internet else target[0]  # type: ignore[index]
        back_to = fmt(net.addr_of(legs[0][0]))
        sport = net.ephemeral()
        self.log_msg(f"[b]TRACEROUTE[/] {back_to} ({src}) → {label}, max {self.TRACE_HOPS} hops")
        reached = min(len(legs), self.TRACE_HOPS)
        for k in range(1, reached + 1):
            b = legs[k - 1][1]
            last = k == len(legs) and why is None and not internet
            net.notes.clear()
            try:
                probe = self._hops(legs[:k], nat=(remote, sport, "icmp"))   # TTL k: it runs out at b
            except NetError as e:
                self.log_msg(f"  {k:>2}  *  [dim](the probe was lost: {escape(str(e))})[/]")
                continue
            what = "echo reply" if last else "time exceeded"
            try:
                _, back, _, _ = self._plan(b[0], back_to, reply=True, remote=remote)
            except NetError as e:
                self.log_msg(f"  {k:>2}  *  [dim]({b[0]}'s {what} can't get back: {escape(str(e))})[/]")
                continue
            rtt = sum(random.uniform(0.15, 0.6) for _ in self._flatten(probe) + back)
            self.log_msg(f"  {k:>2}  {b[0]} {fmt(net.addr_of(b))}  {rtt:.2f} ms" + ("  [green](reached)[/]" if last else ""))
        if len(legs) > self.TRACE_HOPS:
            self.log_msg(f"  [dim]… stopped at {self.TRACE_HOPS} hops[/]")
        elif why is not None:
            self.log_msg(f"  {len(legs) + 1:>2}  *  [dim]({legs[-1][1][0]} can't forward it: {escape(why)})[/]")
        elif internet:
            self.log_msg("  [dim]… then the internet (beyond what ByteSim models)[/]")
        self.canvas.send_packet(self._cells(self._flatten(legs[:reached])), "red", None)

    @command("ping")
    def cmd_ping(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) >= 2 and (args[0].lower() in ("all", "*") or len(args) > 2 or args[1].lower() in ("all", "*")
                               or args[1].lower() in self.net.groups or args[1].lower().split("@")[0] in ("all", "*")):
            self.cmd_mping(args, opts)
            return
        if len(args) != 2:
            raise NetError("usage: ping <src> <id|(net,host)|internet|group|all> [...]")
        src, port = self._src(args[0])
        if args[1].lower() not in self.INTERNET and (
                self._ref_or_none(args[1]) == src or (self._overlay_dst(args[1]) or ("", ""))[1] == src):
            self.log_msg(f"{src}: reply from self (loopback)")
            return
        try:
            label, path, legs, internet = self._plan(src, args[1], port=port)
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
        self._learn(legs)
        carried = self._carried
        c = self.canvas
        links = [li for _, li in path[1:]]
        c.heat(links)

        def arrived() -> None:
            if (why := self._gone(path)) is not None:
                c.cool(links)
                self.log_msg(f"  [red]✗ no reply from {label}: {escape(why)}[/]")
                self.refresh_sidebar()
                return
            c.flash_device(path[-1][0], "red")
            # The echo reply is its own packet: the destination routes it back to
            # the interface that asked, which can fail even though the request arrived.
            try:
                if legs is None:                     # graph mode: cables carry either way
                    back, back_legs = self._reverse(path), None
                elif carried:                        # back through the subnet router, exit node or tunnel
                    back_legs = [(b, a, self._reverse(p)) for a, b, p in reversed(legs)]
                    hops = [hop for leg in back_legs for hop in leg[2]]
                    if down := next((d for d, _ in hops if not self.net.devices[d].up), None):
                        raise NetError(f"{down} went down while the request was out")
                    if any(li >= 0 and not self.net.usable(self.net.links[li]) for _, li in hops):
                        raise NetError("a cable on the way back failed while the request was out")
                    back = self._flatten(back_legs)
                else:
                    _, back, back_legs, _ = self._plan(path[-1][0], fmt(self.net.addr_of(legs[0][0])), reply=True,
                                                       remote="internet" if internet else None)
            except NetError as e:
                if self.trace_enabled:
                    self._log_path_trace_failure("REPLY", label, src, e)
                c.cool(links)
                self.log_msg(f"  [red]✗ no reply from {label}: {escape(str(e))}[/]")
                self.refresh_sidebar()
                return
            if self.trace_enabled:
                self._log_path_trace("REPLY", label, src, back, back_legs,
                                      dst_iface=back_legs[-1][1] if back_legs else None)
            self._learn(back_legs)
            ttl = TTL - self._routers_in(back, back_legs)
            rtt = sum(random.uniform(0.15, 0.6) for _ in path + back) + (12 if internet else 0)

            def replied() -> None:
                c.cool(links)
                if (why := self._gone(back)) is not None:
                    self.log_msg(f"  [red]✗ reply from {label} lost: {escape(why)}[/]")
                    self.refresh_sidebar()
                    return
                c.flash_device(src, "green")
                metrics = (f": hops={len(back) - 1} ttl={ttl} time={rtt:.2f} ms"
                           if self._show_detail() else "")
                self.log_msg(f"  [green]reply from {label}[/]{metrics}")
                self.refresh_sidebar()

            c.send_packet(self._cells(back), "green", replied)

        c.send_packet(self._cells(path), "red", arrived)

    def _gone(self, path: list[tuple[str, int]]) -> str | None:
        """Why a packet's receiver can't take it, if it went down or was removed while
        the packet was on its way (None if it's there). Where the packet was when a cable
        failed isn't known, so it counts as sent once it left."""
        d = path[-1][0]
        if d not in self.net.devices:
            return f"{d} was removed while the packet was on its way"
        if not self.net.devices[d].up:
            return f"{d} went down while the packet was on its way"
        return None

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
        src, port = self._src(args[0])
        if self._ref_or_none(args[1]) == src:
            raise NetError("source and destination are the same device")
        try:
            label, path, legs, internet = self._plan(src, args[1], port=port, stream=True)
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
            if (why := self._gone(path)) is not None:
                self.log_msg(f"  [red]✗ lost on the way to {label}: {escape(why)}[/]")
                self.refresh_sidebar()
                return
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
        src, port = self._src(args[0])
        receivers = [d for d in self._targets(args[1:], whole_network) if self._dev_of(d) != src]
        if not receivers:
            raise NetError("no receivers (the source can't send to itself)")
        bulk = len(receivers) >= self.BULK_PAIRS
        self._prepare_multicast(self._multicast_steps(src, receivers, echo, bulk, port), len(receivers))

    def _multicast_steps(self, src: str, receivers: list[str], echo: bool, bulk: bool,
                         port: int | None = None) -> Iterator[None]:
        """Yield between route queries and packet creation so large batches stay interactive."""
        def detailed() -> bool:
            return self._show_detail(bulk)

        net = self.net
        paths: dict[str, list[tuple[str, int]]] = {}
        reply_targets: dict[str, str] = {}
        endpoints: dict[str, tuple[tuple[str, int], tuple[str, int]]] = {}
        lost: list[str] = []
        carried: dict[str, list[Leg]] = {}           # replies to these retrace the overlay's path
        relays: dict[str, str | None] = {}           # the relay a tunnel went through, if any
        for d in receivers:
            try:
                _, path, legs, _ = self._plan(src, d, port=port)
                if self._carried and legs:
                    carried[d], relays[d] = legs, self._relay
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
                    got = [d for d in paths if d not in reply_failures]
                    names = ": " + " ".join(got) if detailed() else ""
                    self.log_msg(f"  [bright_magenta]delivered to {len(got)} receiver{'s' if len(got) != 1 else ''}[/]{names}")
                self.refresh_sidebar()

        def arrived(dst: str) -> None:
            dst_id = self._dev_of(dst)
            if (why := self._gone(paths[dst])) is not None:
                reply_failures.append(dst)
                if detailed() or len(reply_failures) <= 5:
                    self.log_msg(f"  [red]✗ no reply from {dst} to {src}: {escape(why)}[/]")
                finished()
                return
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
                    reply_label = self._retrace_context(dst, dst_id, src, legs, relays.get(dst))
                else:
                    _, back, legs, _ = self._plan(dst_id, reply_targets[dst], reply=True)
                    reply_label = src
            except NetError as e:
                if self.trace_enabled:
                    self._log_path_trace_failure("REPLY", dst, src, e)
                reply_failures.append(dst)
                if detailed() or len(reply_failures) <= 5:
                    self.log_msg(f"  [red]✗ no reply from {dst} to {src}: {escape(str(e))}[/]")
                finished()
                return
            if self.trace_enabled:
                self._log_path_trace("REPLY", dst, src, back, legs, dst_iface=legs[-1][1] if legs else None)
            self._learn(legs)
            if detailed():
                self._log_route("ECHO REPLY", "green", dst_id, reply_label, back, legs, False)
            links = [li for _, li in back[1:]]
            c.heat(links)
            ttl = TTL - self._routers_in(back, legs)
            rtt = sum(random.uniform(0.15, 0.6) for _ in paths[dst] + back)

            def replied() -> None:
                c.cool(links)
                if (why := self._gone(back)) is not None:
                    reply_failures.append(dst)
                    if detailed() or len(reply_failures) <= 5:
                        self.log_msg(f"  [red]✗ reply from {dst} lost: {escape(why)}[/]")
                    finished()
                    return
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
            _, path, legs, _ = self._plan(src, dst, stream=True)
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
