"""Addresses: keeping them in step with the cabling, DHCP, ad-hoc, static config."""
from __future__ import annotations

from rich.markup import escape


from ...model import Device, Iface, NetError, fmt
from ..inspector import mode_text
from .base import CommandHost
from .registry import command


class AddressCommands(CommandHost):
    @command("dhcp")
    def cmd_dhcp(self, args: list[str], opts: dict[str, str]) -> None:
        self._need_addr_mode("dhcp")
        if not args:
            raise NetError("usage: dhcp <id[:nic]|all> ...")
        for nic in self._nics(args):
            self._dhcp_exchange(nic)
        self.refresh_sidebar()

    @command("release")
    def cmd_release(self, args: list[str], opts: dict[str, str]) -> None:
        if not args:
            raise NetError("usage: release <id[:nic]|all> ...")
        self._need_addr_mode("release")
        for nic in self._nics(args):
            ifc = self.net.iface(*nic)
            if ifc.addr:
                self.log_msg(f"[#ffd700]DHCP[/] {self._ifname(nic)} RELEASE {fmt(ifc.addr)}")
            ifc.mode = "none"
            ifc.clear()
        self.changed()

    @command("adhoc")
    def cmd_adhoc(self, args: list[str], opts: dict[str, str]) -> None:
        if not args:
            raise NetError("usage: adhoc <id[:nic]|all> ...")
        self._need_addr_mode("adhoc")
        for nic in self._nics(args):
            ifc = self.net.iface(*nic)
            ifc.mode = "adhoc"
            ifc.clear()
            self.net.adhoc(nic)
            self.log_msg(f"{self._ifname(nic)} → {fmt(ifc.addr)} [yellow](ad-hoc: free host "
                         f"on network {ifc.net}, no server)[/]")
        self.changed()

    @command("dhcpd")
    def cmd_dhcpd(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args:
            for d in net.sorted_devices():
                if d.kind in "rg":
                    self.log_msg(f"{d.id}: DHCP server {'[red]off[/]' if d.id in net.dhcp_off else '[green]on[/]'}")
            return
        did = net.get(self._ref(args[0])[0]).id
        if not net.is_router(did):
            raise NetError("only routers and gateways run a DHCP server")
        if len(args) > 1:
            if args[1].lower() not in ("on", "off"):
                raise NetError("usage: dhcpd <router> [on|off]")
            (net.dhcp_off.discard if args[1].lower() == "on" else net.dhcp_off.add)(did)
        self.log_msg(f"{did}: DHCP server {'[red]off[/]' if did in net.dhcp_off else '[green]on[/]'}")
        self.changed()

    def reconcile(self, skip: frozenset[tuple[str, int]] = frozenset()) -> None:
        """Bring STP and addresses in line with the cabling, and say what changed."""
        for note in self.net.stp_update():
            color = "green" if note.startswith("broadcast storm over") else "cyan"
            self.log_msg(f"[{color}]{escape(note)}[/]")
        if self.graph:
            return
        notes = self.net.reconcile(skip)       # STP is already current, so these are addresses
        if len(notes) > 4:
            self.log_msg(f"[dim]auto-config: {len(notes)} address changes (type [b]ip[/b] to list)[/]")
        else:
            for n in notes:
                self.log_msg(f"[dim]{escape(n)}[/]")

    def _need_addr_mode(self, what: str) -> None:
        if self.graph:
            raise NetError(f"{what} needs addressing: switch with `mode addr`")

    def _nics(self, toks: list[str], kinds: str = "c") -> list[tuple[str, int]]:
        """Expand c1 / c1:2 / all into cabled NICs."""
        net = self.net
        out: list[tuple[str, int]] = []
        for t in toks:
            if t.lower() in ("all", "*"):
                devs = [d for d in net.sorted_devices() if d.kind in kinds]
                out += [(d.id, p) for d in devs for p in range(1, d.nports + 1)
                        if net.link_index(d.id, p) is not None]
                continue
            did, port = self._ref(t)
            dev = net.get(did)
            if dev.kind not in kinds:
                raise NetError(f"{did} is a {dev.name}: this only applies to computers"
                               + (" (routers serve DHCP: see dhcpd)" if dev.kind in "rg" else ""))
            if port is not None:
                if not 1 <= port <= dev.nports:
                    raise NetError(f"{did} has no port {port}")
                out.append((did, port))
            else:
                out += [(did, p) for p in range(1, dev.nports + 1)
                        if net.link_index(did, p) is not None] or [(did, 1)]
        return out

    def _dhcp_exchange(self, nic: tuple[str, int]) -> None:
        """Animated DISCOVER (broadcast) then OFFER/ACK (unicast), or ad-hoc fallback."""
        net, c = self.net, self.canvas
        name = self._ifname(nic)
        ifc = net.iface(*nic)
        if net.link_index(*nic) is None:
            self.log_msg(f"[yellow]{name}: no cable, so no DHCP[/]")
            return
        ifc.mode = "dhcp"
        mac = net.devices[nic[0]].mac(nic[1])
        if net.storm_near(nic[0]):
            self.log_msg(f"[b #ffd700]DHCP[/] {name} [red]DISCOVER lost: the links are saturated"
                         f"{self.storm_hint(nic[0])}[/]")  # type: ignore[attr-defined]
            return
        fl = net.flood(nic)
        if fl.storm:
            self.log_msg(f"[b #ffd700]DHCP[/] {name} [dim]{mac}[/] DISCOVER → broadcast")
            self.set_off_storm(fl, f"the DHCP DISCOVER from {name}")  # type: ignore[attr-defined]
            self.log_msg("  [red]DHCP failed: the DISCOVER drowned in the storm it caused[/]")
            return
        kind, server = net.lease(nic)
        net.learn_flood(fl)
        reached = len(fl.delivered)
        self.log_msg(f"[b #ffd700]DHCP[/] {name} [dim]{mac}[/] DISCOVER → broadcast "
                     f"(reaches {reached} interface{'s' if reached != 1 else ''})")

        def done(back: list[tuple[str, int]] | None) -> None:
            if back:
                c.cool([li for _, li in back[1:]])
            c.flash_device(nic[0], "#ffd700" if server else "yellow")
            if kind == "adhoc":
                self.log_msg(f"  [yellow]no DHCP server answered: took free address "
                             f"{fmt(ifc.addr)} on network {ifc.net} (ad-hoc)[/]")
            else:
                verb = "ACK (renewed)" if kind == "renew" else "OFFER → REQUEST → ACK"
                self.log_msg(f"  [#ffd700]{verb}[/] from {server[0]} {fmt(net.addr_of(server))}: "
                             f"network {ifc.net} → host {ifc.host}, gateway {ifc.gw}")
            self.refresh_sidebar()
            c.refresh()

        def discovered() -> None:
            if server is None:
                done(None)
                return
            back = net.l2path(server, nic)
            net.learn(server, back)
            c.heat([li for _, li in back[1:]])
            c.send_packet(self._cells(back), "green", lambda: done(back))

        if not fl.generations:
            done(None)
        else:
            self.animate_flood(fl, "#ffd700", discovered)  # type: ignore[attr-defined]

    @command("ip")
    def cmd_ip(self, args: list[str], opts: dict[str, str]) -> None:
        self._need_addr_mode("ip")
        net = self.net
        if not args:
            self.log_msg("[b]DEVICE    MAC     NET  HOST  MODE       GW[/]")
            for d in net.sorted_devices():
                for p in range(1, d.nports + 1):
                    ifc = net.ifaces.get(f"{d.id}:{p}")
                    if ifc and (ifc.addr or (d.kind == "c" and net.link_index(d.id, p) is not None)):
                        self._log_iface(d, p, ifc)
            if not net.ifaces:
                self.log_msg("[dim]no addresses yet: cable a computer to a router or switch[/]")
            return
        did, port = self._ref(args[0])
        dev = net.get(did)
        if dev.kind == "s":
            raise NetError(f"{did} is a switch: layer 2 only, no addresses (its MAC table is in the inspector)")
        if len(args) == 1:
            for p in ([port] if port else range(1, dev.nports + 1)):
                self._log_iface(dev, p, net.iface(did, p))
            return
        if port is None:
            if dev.nports != 1:
                raise NetError(f"{did} has {dev.nports} ports: say which, e.g. {did}:1")
            port = 1
        nic, ifc = (did, port), net.iface(did, port)
        what = args[1].lower()
        if what == "dhcp" or (what == "auto" and dev.kind == "c"):
            if dev.kind != "c":
                raise NetError("only computers are DHCP clients")
            self._dhcp_exchange(nic)
        elif what == "adhoc" and dev.kind == "c":
            ifc.mode = "adhoc"
            ifc.clear()
            net.adhoc(nic)
            self.log_msg(f"{self._ifname(nic)} → {fmt(ifc.addr)} [yellow](ad-hoc)[/]")
        elif what == "auto":
            ifc.mode = "auto"
            ifc.clear()
            self.log_msg(f"{self._ifname(nic)} back to automatic addressing")
        elif what in ("none", "clear", "off"):
            ifc.mode = "none"
            ifc.clear()
            self.log_msg(f"{self._ifname(nic)} unconfigured")
        else:
            usage = "usage: ip <id:port> (net,host) [gateway-host] | dhcp | adhoc | none"
            try:
                addr = self._parse_addr(args[1])
                gw = int(args[2]) if len(args) > 2 else None
            except ValueError:
                raise NetError(usage) from None
            if addr is None:
                raise NetError(usage)
            if ov := next((o for o, t in net.overlays.items() if t["net"] == addr[0]), None):
                raise NetError(f"network {addr[0]} is overlay {ov}'s: pick another network")
            owner = net.resolve(addr)
            if owner and owner != nic:
                raise NetError(f"{fmt(addr)} is already used by {self._ifname(owner)} (ARP conflict)")
            ifc.mode, ifc.via = "static", "static"
            ifc.net, ifc.host = addr
            ifc.gw = gw if dev.kind == "c" else None
            self.log_msg(f"{self._ifname(nic)} → {fmt(addr)}"
                         + (f" gateway {fmt((addr[0], gw))}" if ifc.gw else "") + " [cyan](static)[/]")
        self.changed()

    def _log_iface(self, d: Device, p: int, ifc: Iface) -> None:
        cabled = self.net.link_index(d.id, p) is not None
        net = str(ifc.net) if ifc.addr else "—"
        host = str(ifc.host) if ifc.addr else "—"
        gw = str(ifc.gw) if ifc.gw is not None else ""
        state = "" if cabled else " [dim](no cable)[/]"
        self.log_msg(f"[{d.color}]{d.id + ':' + d.port_name(p):<9}[/] {d.mac(p):<7} {net:>3}  {host:>4}  "
                     f"{mode_text(ifc):<10} {gw}{state}")
