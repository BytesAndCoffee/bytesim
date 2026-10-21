"""Building the network: devices, cables, roles, failures, files, modes."""
from __future__ import annotations

from pathlib import Path

from ... import demos
from ...constants import ALIASES, KINDS, REF, ROLES
from ...model import Link, NetError, Network
from ...model.macros import macro_pairs
from ...model.layout import LAYOUT_CHOICES, LAYOUT_MODES, ORIENTATIONS
from ...topology import dump_topology, load_topology, looks_like_topology
from ..help import HELP
from ..theme import ROLE_COLOR
from .base import CommandHost
from .registry import command

DEFAULT_FILE = "bytesim.json"


class TopologyCommands(CommandHost):
    @command("help", "?", "h")
    def cmd_help(self, args: list[str], opts: dict[str, str]) -> None:
        self.log_msg(HELP)

    @command("add", "new", "+")
    def cmd_add(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args or args[0].lower() not in ALIASES:
            raise NetError("usage: add <g|r|s|c> [ports] [count]")
        kind = ALIASES[args[0].lower()]
        try:
            nports = int(args[1]) if len(args) > 1 else None
            count = int(args[2]) if len(args) > 2 else 1
        except ValueError:
            raise NetError("ports and count must be numbers") from None
        if not 1 <= count <= 100:
            raise NetError("count must be 1..100")
        made = []
        for _ in range(count):
            d = net.add(kind, nports)
            if not self.auto:
                net.place_new(d)
            made.append(d)
        kindword = "NIC" if kind == "c" else "port"
        n = made[0].nports
        self.log_msg(f"[green]+[/] {KINDS[kind][0]} {' '.join(d.id for d in made)} "
                     f"({n} {kindword}{'s' if n != 1 else ''})")
        self.changed()
        self.canvas.select(made[-1].id)

    @command("link", "connect", "ln", "cable")
    def cmd_link(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if len(args) != 2:
            raise NetError("usage: link <id>[:port] <id>[:port] [role=...]")
        role = opts.get("role", "data")
        if role not in ROLES:
            raise NetError(f"role must be one of: {', '.join(ROLES)}")
        (a, pa), (b, pb) = self._ref(args[0]), self._ref(args[1])
        l = net.connect(a, pa, b, pb, role)
        da, db = net.devices[l.a], net.devices[l.b]
        self.log_msg(f"[green]⇄[/] [{da.color}]{l.a}[/]:{da.port_name(l.pa)} ── "
                     f"[{db.color}]{l.b}[/]:{db.port_name(l.pb)}"
                     + (f" [{ROLE_COLOR[role]}]{role}[/]" if role != "data" else ""))
        if not self.replaying:
            self.lag_hint(l.a, l.b)  # type: ignore[attr-defined]
        # Plugging in a computer kicks off a (visible) DHCP exchange.
        ends = [(d, p) for d, p in ((da, l.pa), (db, l.pb))
                if d.kind == "c" and net.iface(d.id, p).mode == "dhcp" and not self.graph
                and not self.replaying]          # scripts re-address once, at the end
        self.changed(skip=frozenset((d.id, p) for d, p in ends))
        for d, p in ends:                 # by Device: auto-renumber may have renamed it
            self._dhcp_exchange((d.id, p))  # type: ignore[attr-defined]

    @command("unlink", "disconnect", "cut")
    def cmd_unlink(self, args: list[str], opts: dict[str, str]) -> None:
        if not 1 <= len(args) <= 2:
            raise NetError("usage: unlink <id>[:port] [id]")
        a, pa = self._ref(args[0])
        b = self._ref(args[1])[0] if len(args) == 2 else None
        n = self.net.disconnect(a, pa, b)
        self.log_msg(f"[yellow]✂[/] removed {n} cable{'s' if n != 1 else ''}" if n
                     else "[dim]no matching cables[/]")
        self.changed()

    @command("del", "rm", "delete", "remove")
    def cmd_del(self, args: list[str], opts: dict[str, str]) -> None:
        if not args:
            raise NetError("usage: del <id> [id ...]")
        for tok in args:
            did, _ = self._ref(tok)
            n = self.net.remove(did)
            self.log_msg(f"[red]−[/] {did} deleted ({n} cable{'s' if n != 1 else ''} removed)")
        self.changed()

    @command("ports", "nics", "resize")
    def cmd_ports(self, args: list[str], opts: dict[str, str]) -> None:
        if args and args[0].lower() == "copy":     # ports copy src(old, …) dst(new, …): cabling only
            self.copy_cabling(args[1:])  # type: ignore[attr-defined]
            return
        if len(args) < 2 or not args[-1].isdigit():
            raise NetError("usage: ports <id…> <count>   (e.g. ports s1 s2 s3 7)")
        ids = [self.net.get(self._ref(t)[0]).id for t in args[:-1]]
        for did in ids:
            self.net.resize(did, int(args[-1]))
        self.log_msg(f"{' '.join(ids)} now {'has' if len(ids) == 1 else 'have'} {args[-1]} ports")
        self.changed()

    @command("mesh", "zip", "chain", "ring", "parallel")
    def cmd_macro(self, args: list[str], opts: dict[str, str]) -> None:
        """Cabling macros: mesh A B · zip A B · chain x… · ring x… · parallel a b n.
        A name is a device (r1, r1:3), a group (all its devices, in order), or a,b,c."""
        op = self._macro_word
        net = self.net
        role = opts.get("role", "data")
        if role not in ROLES:
            raise NetError(f"role must be one of: {', '.join(ROLES)}")
        usage = {"mesh": "mesh <A> <B>", "zip": "zip <A> <B>", "chain": "chain <x> <y> …",
                 "ring": "ring <x> <y> …", "parallel": "parallel <a> <b> <count>"}[op]
        count = None
        if op == "parallel":
            if len(args) != 3 or not args[2].isdigit():
                raise NetError(f"usage: {usage}")
            count, args = int(args[2]), args[:2]
        if not args or (op in ("mesh", "zip", "parallel") and len(args) != 2):
            raise NetError(f"usage: {usage}   [role=…]")

        def ends(tok: str) -> list[tuple[str, int | None]]:
            out = []
            for part in [p for p in tok.split(",") if p]:
                if part.lower() in net.groups:
                    out += [(d, None) for d in net.devices_in(part.lower())]
                else:
                    did, port = self._ref(part)
                    out.append((net.get(did).id, port))
            return out

        sides = [ends(t) for t in args]
        pairs = macro_pairs(op, sides if op in ("mesh", "zip", "parallel") else [sum(sides, [])], count)
        made = []
        for (a, pa), (b, pb) in pairs:
            l = net.connect(a, pa, b, pb, role)
            made.append(f"{l.a}:{l.pa}–{l.b}:{l.pb}")
        self.log_msg(f"[green]⇄[/] {op}: {len(made)} cable{'s' if len(made) != 1 else ''}"
                     + (f" ({role})" if role != "data" else "") + f"  [dim]{' '.join(made[:8])}"
                     + (" …" if len(made) > 8 else "") + "[/]")
        self.changed()

    @command("role")
    def cmd_role(self, args: list[str], opts: dict[str, str]) -> None:
        role = args[-1].lower() if args else ""
        if len(args) < 2 or role not in ROLES:
            raise NetError(f"usage: role <id[:port]> [id] <{'|'.join(ROLES)}>")
        for l in self._links_matching(args[:-1]):
            l.role = role
            self.log_msg(f"{self._link_text(l)} is now [{ROLE_COLOR[role]}]{role}[/]")
        self.changed()

    @command("fail", "down")
    def cmd_fail(self, args: list[str], opts: dict[str, str]) -> None:
        self._fail(args, up=False)

    @command("restore", "up", "repair")
    def cmd_restore(self, args: list[str], opts: dict[str, str]) -> None:
        self._fail(args, up=True)

    @command("mode")
    def cmd_mode(self, args: list[str], opts: dict[str, str]) -> None:
        if not args or args[0].lower() not in ("graph", "addr", "ip", "address"):
            self.log_msg(f"mode: [b]{'graph' if self.graph else 'addr'}[/]  (mode graph | mode addr)")
            return
        self.graph = args[0].lower() == "graph"
        self.log_msg("[b]graph mode[/]: traffic follows cables only, addresses ignored" if self.graph
                     else "[b]address mode[/]: networks, gateways and DHCP apply")
        self.changed()

    @command("auto", "arrange", "layout")
    def cmd_auto(self, args: list[str], opts: dict[str, str]) -> None:
        args = [a.lower() for a in args]
        if args == ["list"]:
            self.log_msg(f"layout: [b]{self.net.layout_mode}[/] · available: {', '.join(LAYOUT_MODES)}"
                         + f" · orientation: [b]{self.net.group_orientation}[/]"
                         + f" · orientations: {', '.join(ORIENTATIONS)}")
            return
        fields = {value: field for field, choices in LAYOUT_CHOICES.items() for value in choices}
        updates: dict[str, str] = {}
        for arg in args:
            field = fields.get(arg)
            if field is None or field in updates:
                raise NetError(f"usage: layout [{'|'.join(LAYOUT_MODES)}] [{'|'.join(ORIENTATIONS)}]"
                               " (either order, at most one of each) · layout list")
            updates[field] = arg
        # Validate the whole command before changing either setting.
        for field, value in updates.items():
            setattr(self.net, field, value)
        self.arrange(announce=True)  # type: ignore[attr-defined]

    @command("list", "ls")
    def cmd_list(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        for d in net.sorted_devices():
            used = sum(1 for l in net.links if l.touches(d.id))
            self.log_msg(f"[{d.color}]{d.id:<4}[/] {d.name:<8} {used}/{d.nports} "
                         f"{'NICs' if d.kind == 'c' else 'ports'}")
        for l in net.links:
            self.log_msg("  " + self._link_text(l))
        if net.groups:
            self.dispatch(["groups"])
        if not net.devices:
            self.log_msg("[dim]empty network[/]")

    @command("clear")
    def cmd_clear(self, args: list[str], opts: dict[str, str]) -> None:
        self.replace_network(Network(), auto=True)  # type: ignore[attr-defined]
        self.log_msg("[yellow]network cleared[/]")

    @command("demo")
    def cmd_demo(self, args: list[str], opts: dict[str, str]) -> None:
        name = args[0].lower() if args else "basic"
        if name not in demos.DEMOS:
            raise NetError(f"demos: {', '.join(demos.DEMOS)}")
        net, hint = demos.DEMOS[name]()
        self.replace_network(net, auto=True)  # type: ignore[attr-defined]
        self.log_msg(hint)

    @command("save")
    def cmd_save(self, args: list[str], opts: dict[str, str]) -> None:
        path = Path(args[0] if args else DEFAULT_FILE).expanduser()
        try:
            path.write_text(self.net.to_json())
        except OSError as e:
            raise NetError(f"couldn't save {path}: {e}") from None
        self.log_msg(f"saved to {path}")

    @command("load", "open")
    def cmd_load(self, args: list[str], opts: dict[str, str]) -> None:
        path = Path(args[0] if args else DEFAULT_FILE).expanduser()
        try:
            text = path.read_text()
            topology = looks_like_topology(text)
            net = load_topology(text) if topology else Network.from_json(text)
        except NetError as e:
            raise NetError(f"{path}: {e}") from None
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise NetError(f"couldn't load {path}: {e}") from None
        # A topology has no positions, so it's laid out; a save file keeps its own.
        self.replace_network(net, auto=topology)  # type: ignore[attr-defined]
        self.log_msg(f"loaded {path} ({len(net.devices)} devices, {len(net.links)} cables"
                     + (", topology: auto-arranged)" if topology else ")"))

    @command("export")
    def cmd_export(self, args: list[str], opts: dict[str, str]) -> None:
        if args and args[0].lower() == "script":            # export script [mode] [file]
            self.export_script(args[1:])  # type: ignore[attr-defined]
            return
        if not 1 <= len(args) <= 2 or (len(args) == 2 and args[1].lower() != "ports"):
            raise NetError("usage: export <file.yaml> [ports]   (ports = pin every cable to its ports)")
        path = Path(args[0]).expanduser()
        pin = len(args) == 2
        try:
            path.write_text(dump_topology(self.net, pin_ports=pin))
        except OSError as e:
            raise NetError(f"couldn't write {path}: {e}") from None
        self.log_msg(f"exported topology to {path}"
                     + (" (every port pinned)" if pin else " (compact: meshes folded, ports allocated on load)"))

    @command("quit", "exit", "q")
    def cmd_quit(self, args: list[str], opts: dict[str, str]) -> None:
        self.exit()  # type: ignore[attr-defined]

    # ---- helpers -------------------------------------------------------
    def _link_text(self, l: Link, state: bool = True) -> str:
        net = self.net
        txt = f"{l.a}:{net.devices[l.a].port_name(l.pa)} ── {l.b}:{net.devices[l.b].port_name(l.pb)}"
        if l.role != "data":
            txt += f" [{ROLE_COLOR[l.role]}]{l.role}[/]"
        return txt + (" [red]✕ down[/]" if state and not l.up else "")

    def _links_matching(self, args: list[str]) -> list[Link]:
        """Cables picked by `id:port` or `id[:port] id[:port]`."""
        if not 1 <= len(args) <= 2:
            raise NetError("pick a cable with id:port, or two device ids")
        (a, pa) = self._ref(args[0])
        self.net.get(a)
        b, pb = self._ref(args[1]) if len(args) == 2 else (None, None)
        if b is None and pa is None:
            raise NetError(f"which cable of {a}? use {a}:<port> or name the other end")
        hit = [l for l in self.net.links if l.touches(a)
               and (pa is None or l.port_of(a) == pa)
               and (b is None or (l.other(a)[0] == b and (pb is None or l.other(a)[1] == pb)))]
        if not hit:
            raise NetError("no matching cable")
        return hit

    def _fail(self, args: list[str], up: bool) -> None:
        net = self.net
        verb = "restored" if up else "failed"
        if not args:
            raise NetError(f"usage: {'restore' if up else 'fail'} <id> | <id:port> | <id> <id>"
                           + (" | all" if up else ""))
        if up and args[0].lower() == "all":
            for d in net.devices.values():
                d.up = True
            for l in net.links:
                l.up = True
            self.log_msg("[green]✓ everything restored[/]")
        elif len(args) == 1 and REF.match(args[0]) and self._ref(args[0])[1] is None:
            d = net.get(self._ref(args[0])[0])
            d.up = up
            self.log_msg(f"[{'green' if up else 'red'}]{'✓' if up else '✕'} {d.id} {verb}[/]")
        else:
            for l in self._links_matching(args):
                l.up = up
                self.log_msg(f"[{'green' if up else 'red'}]{'✓' if up else '✕'}[/] cable "
                             f"{self._link_text(l, state=False)} {verb}")
        net.flush_macs()                  # link down: switches forget what was learned there
        self.changed()
