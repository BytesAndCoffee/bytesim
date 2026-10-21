"""The public internet between sites, and NAT on the gateways that face it."""
from __future__ import annotations

from ...model import NetError
from ...model.types import fmt
from .base import CommandHost
from .registry import command


class WanCommands(CommandHost):
    @command("internet")
    def cmd_internet(self, args: list[str], opts: dict[str, str]) -> None:
        """internet: show · internet <group>: that group is the public internet · internet off."""
        net = self.net
        if not args:
            self._show_internet()
            return
        if len(args) != 1:
            raise NetError("usage: internet  ·  internet <group>  ·  internet off")
        g = args[0].lower()
        if g in ("off", "none"):
            if net.internet is None:
                raise NetError("no group is the internet")
            self.log_msg(f"{net.internet} is an ordinary group again: no WAN ports, no NAT")
            net.internet = None
            self.changed()
            return
        if g not in net.groups:
            raise NetError(f"no group {g}")
        net.internet = g
        if not any(net.devices[d].kind == "g" for d in net.devices_in(g)):
            self.log_msg(f"[yellow]{g} has no gateway yet: nothing reaches the internet until it has "
                         f"one (an ISP edge)[/]")
        self.changed()
        self._show_internet()

    def _show_internet(self) -> None:
        net = self.net
        if net.internet is None:
            self.log_msg("[dim]no internet: internet <group> makes a group (ISP gateways, routers, "
                         "public servers) the public internet, and gateways cabled to it run NAT[/]")
            return
        self.log_msg(f"[magenta]☁ internet[/] = {net.internet}: {' '.join(net.devices_in(net.internet))}")
        sites = [d.id for d in net.sorted_devices() if net.wan_ports(d.id)]
        if not sites:
            self.log_msg("  [dim]no gateway is cabled to it yet[/]")
        for g in sites:
            dev = net.devices[g]
            wan = ", ".join(f"{dev.port_name(p)} {fmt(net.addr_of((g, p)))}" for p in sorted(net.wan_ports(g)))
            nat = ((f"[green]{'hard' if g in net.nat_hard else 'easy'} NAT[/]") if net.natting(g)
                   else "[yellow]NAT off[/] (inside is reachable)")
            self.log_msg(f"  {g}: WAN {wan} · {nat}")

    @command("nat")
    def cmd_nat(self, args: list[str], opts: dict[str, str]) -> None:
        """nat: list · nat <gateway…> on|off · nat <gateway…> easy|hard."""
        net = self.net
        if not args:
            self._show_internet()
            return
        state = args[-1].lower()
        if state not in ("on", "off", "easy", "hard") or len(args) < 2:
            raise NetError("usage: nat  ·  nat <gateway…> on|off  ·  nat <gateway…> easy|hard")
        gws = []
        for tok in args[:-1]:
            dev = net.get(self._ref(tok)[0])  # type: ignore[attr-defined]
            if dev.kind != "g":
                raise NetError(f"{dev.id} is not a gateway: only gateways run NAT")
            gws.append(dev.id)
        for g in gws:
            note = ("" if net.wan_ports(g) else
                    f" [dim]({g} is part of the internet ({net.internet}), so it never translates)[/]" if g in net.public()
                    else " [dim](no WAN port yet: it applies once cabled to the internet)[/]")
            if state in ("easy", "hard"):
                (net.nat_hard.add if state == "hard" else net.nat_hard.discard)(g)
                what = ("a new public port per destination: hole punching to another hard NAT fails"
                        if state == "hard" else "one public port per inside flow: hole punching works")
                self.log_msg(f"{g}: {state} NAT [dim]({what})[/]{note}")
                continue
            (net.nat_off.discard if state == "on" else net.nat_off.add)(g)
            self.log_msg(f"{g}: NAT {state}{note}")
        self.changed()
