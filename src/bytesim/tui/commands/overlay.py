"""Overlays: mesh VPNs between their members, reaching each other through NAT."""
from __future__ import annotations

from ...constants import GROUP_NAME, REF
from ...model import NetError
from ...model.overlay import OVERLAY_RESERVED
from ...model.types import fmt
from .base import CommandHost
from .registry import command

OVERLAY_USAGE = ("usage: overlay [<name>]  ·  overlay <name> <device|group…> [host=<n>]  ·  overlay <name> remove <…>  ·  "
                 "overlay <name> advertise|unadvertise <member…>  ·  overlay <name> peers <member>  ·  "
                 "overlay <name> coord=<device>|none exit=<member>|none lan=on|off net=<id>  ·  overlay off <name>")


class OverlayCommands(CommandHost):
    RESERVED = OVERLAY_RESERVED
    SETTINGS = ("coord", "exit", "lan", "net", "host")

    @command("overlay")
    def cmd_overlay(self, args: list[str], opts: dict[str, str]) -> None:
        """Every form checks all of its input before it changes anything."""
        net = self.net
        if not args:
            self._overlay_status()
            return
        if unknown := [k for k in opts if k not in self.SETTINGS]:
            raise NetError(f"overlay doesn't take {', '.join(k + '=' for k in unknown)}: {OVERLAY_USAGE}")
        if args[0].lower() == "off":
            if len(args) != 2 or args[1].lower() not in net.overlays:
                raise NetError("usage: overlay off <overlay>")
            del net.overlays[args[1].lower()]
            self.log_msg(f"overlay {args[1].lower()} removed")
            self.changed()
            return
        name = args[0].lower()
        exists = name in net.overlays
        if not exists:
            if not GROUP_NAME.match(name) or REF.match(name):
                raise NetError(f"bad overlay name {name!r}: start with a letter and must not look like a device id")
            if name in self.RESERVED:
                raise NetError(f"{name!r} is a word the overlay command uses: pick another name")
        verb = args[1].lower() if len(args) > 1 else ""
        if verb == "peers":
            if len(args) != 3 or opts:
                raise NetError(f"usage: overlay {name} peers <member>")
            self._overlay_status(name, peers_of=name, peers_for=[self._member(name, args[2])])
            return
        if verb in ("advertise", "unadvertise"):
            if opts:
                raise NetError(f"usage: overlay {name} {verb} <member…>")
            self._advertise(name, verb == "advertise", args[2:])
            return
        removing = verb == "remove"
        devs = self._devices(args[2:] if removing else args[1:])

        # ---- check everything --------------------------------------------------
        if removing:
            if not exists:
                raise NetError(f"no overlay {name}")
            if not devs or opts:
                raise NetError(f"usage: overlay {name} remove <device|group…>")
        if "net" in opts:
            if exists:
                raise NetError(f"overlay {name} already uses network {net.overlays[name]['net']}: "
                               f"an overlay keeps its network")
            if not opts["net"].isdigit():
                raise NetError("net= takes a network id")
        host = None
        if "host" in opts:
            if len(devs) != 1 or not opts["host"].isdigit():
                raise NetError("host=<n> picks one device's overlay host number: overlay <name> <device> host=<n>")
            host = int(opts["host"])
            if exists and host in net.overlays[name].get("hosts", {}).values():
                raise NetError(f"overlay host {host} is taken in overlay {name}")
        coord = None
        if "coord" in opts and opts["coord"] not in ("none", "off"):
            coord = net.get(self._ref(opts["coord"])[0])
            if coord.kind == "s":
                raise NetError(f"{coord.id} is a switch: it can't run a coordination server")
        members = set(net.overlay_members(name)) if exists else set()
        new = [d for d in devs if d not in members] if not removing else []
        if "exit" in opts and opts["exit"] not in ("none", "off"):
            ex = net.get(self._ref(opts["exit"])[0]).id
            if ex not in members | set(new):
                raise NetError(f"{ex} is not in overlay {name}: only members can route for it")
        if "lan" in opts and opts["lan"] not in ("on", "off"):
            raise NetError("lan=on|off: whether members keep their own site's traffic local with an exit node")
        if new and (coord is not None or (exists and "coord" not in opts)):
            # joining means registering with the coordination server: the one this command
            # sets, or the overlay's current one
            for d in new:
                if (problem := net.can_register(name, d, coord.id if coord is not None else None)) is not None:
                    raise NetError(f"{d} can't join overlay {name}: {problem}")
        if len(members) + len(new) > 254:
            raise NetError(f"overlay {name} would have more members than one network can address (254)")

        # ---- then change it ------------------------------------------------------
        if not exists:
            net.new_overlay(name, int(opts["net"]) if "net" in opts else None)
            self.log_msg(f"overlay {name}: network {net.overlays[name]['net']}")
        t = net.overlays[name]
        if removing:
            notes = [n for d in devs if d in members for n in net.leave_overlay(name, d)]
            self.log_msg(f"overlay {name}: removed {' '.join(d for d in devs if d in members) or 'nobody (not members)'}"
                         + (f" [dim]({'; '.join(notes)})[/]" if notes else ""))
        for d in new:
            net.join_overlay(name, d, host)
        if "coord" in opts:
            if coord is None:
                t.pop("coord", None)
                self.log_msg(f"overlay {name}: no coordination server")
            else:
                t["coord"] = coord.id
                self.log_msg(f"overlay {name}: coordination server {coord.id} [dim](members register through "
                             f"it; tunnels don't pass through it)[/]")
        if "exit" in opts:
            if opts["exit"] in ("none", "off"):
                t.pop("exit", None)
                self.log_msg(f"overlay {name}: no exit node")
            else:
                t["exit"] = net.get(self._ref(opts["exit"])[0]).id
                self.log_msg(f"overlay {name}: exit node {t['exit']} [dim](members' internet and other "
                             f"non-local traffic leaves through it)[/]")
        if "lan" in opts:
            if opts["lan"] == "on":
                t["lan"] = True
            else:
                t.pop("lan", None)
            self.log_msg(f"overlay {name}: with an exit node, members' own-site traffic "
                         + ("stays local" if opts["lan"] == "on" else "goes through it too (the default)"))
        self.changed()
        if not removing:
            self._overlay_status(name, peers_of=name if not (devs or opts) else None)

    def _devices(self, toks: list[str]) -> list[str]:
        """Devices named directly or through a group (whose switches are skipped)."""
        net = self.net
        devs: list[str] = []
        for tok in toks:
            if tok.lower() in net.groups:
                ids = [d for d in net.devices_in(tok.lower()) if net.devices[d].kind != "s"]
            else:
                dev = net.get(self._ref(tok)[0])
                if dev.kind == "s":
                    raise NetError(f"{dev.id} is a switch: layer 2 only, it can't run an overlay client")
                ids = [dev.id]
            devs += [d for d in ids if d not in devs]
        return devs

    def _member(self, name: str, tok: str) -> str:
        net = self.net
        if name not in net.overlays:
            raise NetError(f"no overlay {name}")
        dev = net.get(self._ref(tok)[0]).id
        if dev not in net.overlay_members(name):
            raise NetError(f"{dev} is not in overlay {name}: only members can route for it")
        return dev

    def _advertise(self, name: str, on: bool, toks: list[str]) -> None:
        """Subnet routers: members that carry the overlay's traffic onto their own LAN networks."""
        net = self.net
        if not toks:
            raise NetError(f"usage: overlay {name} advertise|unadvertise <member…>")
        devs = [self._member(name, tok) for tok in toks]       # all checked before any change
        t = net.overlays[name]
        routes = t.setdefault("routes", [])
        for dev in devs:
            if on and dev not in routes:
                routes.append(dev)
                nets = ", ".join(str(n) for n in sorted(net._nets(dev))) or "none yet"
                self.log_msg(f"overlay {name}: {dev} is a subnet router for its LAN (network {nets}): "
                             f"members reach devices there through it")
            elif not on and dev in routes:
                routes.remove(dev)
                self.log_msg(f"overlay {name}: {dev} no longer routes for its LAN")
        if not routes:
            del t["routes"]
        self.changed()

    @command("acl")
    def cmd_acl(self, args: list[str], opts: dict[str, str]) -> None:
        """acl <overlay>: list · acl <overlay> allow <from> <to> · acl <overlay> remove <n> · clear · none."""
        net = self.net
        usage = ("usage: acl <overlay>  ·  acl <overlay> allow <from> <to>  ·  acl <overlay> remove <n>  ·  "
                 "acl <overlay> clear (allow all)  ·  acl <overlay> none (deny all)   (from/to: device, group, * or internet)")
        if not args or args[0].lower() not in net.overlays:
            raise NetError(usage if not args else f"no overlay {args[0].lower()}")
        ov, t = args[0].lower(), net.overlays[args[0].lower()]
        verb = args[1].lower() if len(args) > 1 else "list"
        if verb == "allow" and len(args) == 4:
            rule = []
            for tok in args[2:]:
                x = tok.lower()
                if x not in ("*", "internet") and x not in net.groups:
                    x = net.get(self._ref(tok)[0]).id
                rule.append(x)
            if rule in t.get("acl", []):
                raise NetError(f"overlay {ov} already allows {rule[0]} → {rule[1]}")
            first = "acl" not in t
            t.setdefault("acl", []).append(rule)
            self.log_msg(f"overlay {ov}: allow {rule[0]} → {rule[1]}"
                         + (" [yellow](rules are on: anything not allowed is now denied)[/]" if first else ""))
        elif verb == "remove" and len(args) == 3 and args[2].isdigit():
            rules = t.get("acl", [])
            n = int(args[2])
            if not 1 <= n <= len(rules):
                raise NetError(f"overlay {ov} has {len(rules)} rule{'s' if len(rules) != 1 else ''}")
            a, b = rules.pop(n - 1)
            self.log_msg(f"overlay {ov}: removed allow {a} → {b}"
                         + ("" if rules else f" [yellow](no rules left: everything is denied; acl {ov} clear "
                                             f"allows everything again)[/]"))
        elif verb == "clear" and len(args) == 2:
            t.pop("acl", None)
            self.log_msg(f"overlay {ov}: access rules off, everything allowed")
        elif verb == "none" and len(args) == 2:
            t["acl"] = []
            self.log_msg(f"overlay {ov}: access rules on with nothing allowed: every member is cut off")
        elif verb == "list" and len(args) <= 2:
            rules = t.get("acl", [])
            if "acl" not in t:
                self.log_msg(f"[dim]overlay {ov} has no access rules: every member reaches every other "
                             f"(acl {ov} allow <from> <to> switches to allow-listed only)[/]")
            elif not rules:
                self.log_msg(f"[yellow]overlay {ov}'s access rules allow nothing[/] [dim](acl {ov} clear: allow everything)[/]")
            for i, (a, b) in enumerate(rules, 1):
                self.log_msg(f"  {i}. allow {a} → {b}")
            return
        else:
            raise NetError(usage)
        self.changed()

    @command("relay")
    def cmd_relay(self, args: list[str], opts: dict[str, str]) -> None:
        """relay: list · relay <device…>: run relays · relay off <device…>."""
        net = self.net
        if not args:
            if not net.relays:
                self.log_msg("[dim]no relays: relay <device> runs one (best on the public internet); "
                             "overlay peers that can't go direct relay through it[/]")
            for d in net.relays:
                self.log_msg(f"[#d7af5f]⇄ relay {d}[/]" + ("" if net.devices[d].up else " [red]down[/]"))
            return
        off = args[0].lower() == "off"
        devs = [net.get(self._ref(tok)[0]) for tok in (args[1:] if off else args)]
        if not devs:
            raise NetError("usage: relay  ·  relay <device…>  ·  relay off <device…>")
        if switch := next((d for d in devs if d.kind == "s"), None):
            raise NetError(f"{switch.id} is a switch: it can't run a relay")
        for dev in devs:
            if off:
                net.relays = [d for d in net.relays if d != dev.id]
            elif dev.id not in net.relays:
                net.relays.append(dev.id)
        self.changed()
        self.cmd_relay([], {})

    def _overlay_status(self, only: str | None = None, peers_of: str | None = None,
                        peers_for: list[str] | None = None) -> None:
        net = self.net
        if not net.overlays:
            self.log_msg("[dim]no overlays: overlay <name> <devices…> gives them overlay addresses "
                         "that reach each other through NAT (then: ping c1 c3@<name>)[/]")
            return
        for ov in sorted(net.overlays, key=lambda t: net.overlays[t]["net"]):
            if only and ov != only:
                continue
            members = net.overlay_members(ov)
            t = net.overlays[ov]
            coord, problem = t.get("coord"), net.coord_state(ov)
            self.log_msg(f"[#d7af5f]⌁ overlay {ov}[/]: network {t['net']}, "
                         f"{len(members)} member{'s' if len(members) != 1 else ''}"
                         + (f" · coordination {coord}" if coord else "")
                         + (f" · subnet routers {' '.join(t['routes'])}" if t.get("routes") else "")
                         + (f" · exit node {t['exit']}" + (" (LAN stays local)" if t.get("lan") else "")
                            if t.get("exit") else "")
                         + (f" · {len(t['acl'])} access rule{'s' if len(t['acl']) != 1 else ''}" if "acl" in t else "")
                         + (f" [red]({problem}: nobody can join; existing tunnels keep working)[/]" if problem else ""))
            small = len(members) <= net.PEER_TABLE_MAX
            table = net.peer_table(ov, peers_for) if peers_of == ov and (small or peers_for) else {}
            style = {"direct": "green", "denied": "red", "✗": "red"}
            for d in members:
                if peers_for and d not in peers_for:
                    continue
                real = ", ".join(fmt(i.addr) for _, i in net.addressed(d)) or "[red]no real address[/]"
                state = "" if net.devices[d].up else " [red]down[/]"
                peers = []
                for o in members:
                    if (d, o) in table:
                        how = table[d, o].split(" ", 1)[0] if table[d, o].startswith("✗") else table[d, o]
                        peers.append(f"{o} [{style.get(how, 'yellow')}]{how}[/]")
                where = f" · {net.nat_situation(d)}" if net.devices[d].up else ""
                self.log_msg(f"  {fmt(net.ol_addr(ov, d))} {d}  [dim]endpoint {real}{where}[/]{state}"
                             + (f"  · {', '.join(peers)}" if peers else ""))
            if peers_of == ov and not small and not peers_for:
                self.log_msg(f"  [dim]{len(members)} members: overlay {ov} peers <member> shows how one connects "
                             f"to each of the others[/]")
