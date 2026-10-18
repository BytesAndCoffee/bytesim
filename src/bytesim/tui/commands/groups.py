"""Groups (nestable) and HA clusters."""
from __future__ import annotations

from ...constants import POLICIES, VIP_HOST
from ...model import NetError, fmt
from ..theme import HA_COLOR, group_color
from .base import CommandHost
from .registry import command


def _call(tok: str) -> tuple[str, list[str] | None]:
    """'link2(rfab2,rfab3)' → ('link2', ['rfab2', 'rfab3']); 'link(1)' stays an auto name."""
    if not tok.endswith(")") or "(" not in tok:
        return tok, None
    at = tok.rindex("(")
    name, inner = tok[:at], tok[at + 1:-1]
    if inner.isdigit() and "(" not in name:          # fab(1): auto-numbering, not endpoints
        return tok, None
    ends = [e for e in inner.split(",") if e]
    if not ends or not name:
        raise NetError(f"{tok}: expected name(endpoint, …)")
    return name, [e.lower() for e in ends]


class GroupCommands(CommandHost):
    @command("group")
    def cmd_group(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args:
            self.dispatch(["groups"])
            return
        name = args[0].lower()
        if len(args) == 1:
            if name not in net.groups:
                raise NetError(f"no such group: {name} (create one with group {name} <ids...>)")
            self.canvas.select_group(name)
            self.canvas.focus()
            return
        name = net.resolve_group_name(name)          # fab(1) → the next free fabN
        moved = net.group_add(name, self._members(args[1:]))
        if moved:
            self.log_msg(f"[dim]moved {' '.join(moved)} out of their old group[/]")
        self.log_msg(f"[{group_color(name)}]▣ group {name}[/]: {self._group_text(name)}")
        self.changed()

    @command("clone", "copy", "dup")
    def cmd_clone(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        usage = ("usage: clone <group> [new-name]  ·  clone <group>(old, …) <new-name>(new, …)\n"
                 "  e.g. copy link1(rfab1, rfab2) link2(rfab2, rfab3) creates link2 cabled to rfab2 and rfab3 "
                 "the way link1 is cabled to rfab1 and rfab2")
        if not 1 <= len(args) <= 2:
            raise NetError(usage)
        name, old_ends = _call(args[0])
        new_tok, new_ends = _call(args[1]) if len(args) == 2 else (None, None)
        name = name.lower()
        if name not in net.groups:
            raise NetError(f"no such group: {name}\n{usage}")
        if (old_ends is None) != (new_ends is None):
            raise NetError("give the endpoints on both sides: group(old, …) new(new, …)")
        if old_ends is not None and len(old_ends) != len(new_ends):
            raise NetError(f"{len(old_ends)} old endpoints but {len(new_ends)} new ones: they're paired in order")
        dx = 0
        if not self.auto:                     # manual layout: put the copy just right of the original
            x0, _, x1, _ = net.group_bounds(name)
            dx = x1 - x0 + 3
        new_name = net.resolve_group_name(new_tok) if new_tok else None
        res = net.clone_group(name, new_name, dx=dx, rewire=list(zip(old_ends or [], new_ends or [])))
        devs = " ".join(f"{o}→{n}" for o, n in res.devices.items())
        subs = [f"{o}→{n}" for o, n in res.groups.items() if o != name]
        self.log_msg(f"[{group_color(res.group)}]▣ cloned {name} → {res.group}[/]: {len(res.devices)} devices, "
                     f"{res.cables} internal cable{'s' if res.cables != 1 else ''}"
                     + (f", {res.rewired} rewired to {' '.join(new_ends or [])}" if res.rewired else "")
                     + (f" [dim]({res.skipped} to the outside not copied)[/]" if res.skipped else ""))
        if res.moved:
            self.log_msg(f"  [dim]ports already taken, used the next free: {' '.join(res.moved)}[/]")
        self.log_msg(f"  [dim]{devs}{'  ·  groups ' + ' '.join(subs) if subs else ''}[/]")
        self.changed()
        self.canvas.select_group(res.group)

    def copy_cabling(self, args: list[str]) -> None:
        """ports copy link1(rfab1, rfab2) link2(rfab2, rfab3)"""
        net = self.net
        usage = ("usage: ports copy <group>(old, …) <group>(new, …)\n"
                 "  e.g. ports copy link1(rfab1, rfab2) link2(rfab2, rfab3): cable link2 to rfab2 and rfab3 "
                 "the way link1 is cabled to rfab1 and rfab2")
        if len(args) != 2:
            raise NetError(usage)
        (src, old_ends), (dst, new_ends) = _call(args[0]), _call(args[1])
        if old_ends is None or new_ends is None:
            raise NetError(f"give the endpoints on both sides\n{usage}")
        if len(old_ends) != len(new_ends):
            raise NetError(f"{len(old_ends)} old endpoints but {len(new_ends)} new ones: they're paired in order")
        src, dst = src.lower(), dst.lower()
        if dst not in net.groups and src in net.groups:
            raise NetError(f"no group {dst}: `ports copy` only cables existing devices. To create {dst} "
                           f"with this cabling: copy {args[0]} {args[1]}")
        res = net.copy_cabling(src, dst, list(zip(old_ends, new_ends)))
        pairs = ", ".join(f"{o}→{n}" for o, n in zip(old_ends, new_ends))
        if res.made:
            self.log_msg(f"[green]⇄[/] {dst} now cabled like {src} [dim]({pairs})[/]: "
                         f"{res.made} cable{'s' if res.made != 1 else ''} made"
                         + (f", {res.already} already there" if res.already else ""))
        else:
            self.log_msg(f"{dst} is already cabled like {src} ({pairs}): nothing to do")
        if res.moved:
            self.log_msg(f"  [dim]ports already taken, used the next free: {' '.join(res.moved)}[/]")
        self.changed()

    @command("ungroup")
    def cmd_ungroup(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args:
            raise NetError("usage: ungroup <group> [member ...]")
        name = args[0].lower()
        parent = net.parent_of(name)
        n = net.ungroup(name, self._members(args[1:]) or None)
        where = f"into {parent}" if parent else "to the top level"
        if name not in net.groups:
            self.log_msg(f"[yellow]▢[/] dissolved {name}: {n} member{'s' if n != 1 else ''} moved {where}")
        else:
            self.log_msg(f"[yellow]▢[/] took {n} out of {name}, {where}")
        self.changed()

    @command("groups")
    def cmd_groups(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net

        def tree(g: str, depth: int) -> None:
            devs = [m for m in net.groups[g] if m not in net.groups]
            tag = " [dim](HA cluster)[/]" if g in net.clusters else ""
            self.log_msg(f"{'  ' * depth}[{group_color(g)}]▣ {g}[/]{tag} {' '.join(devs)}")
            for sub in net.subgroups(g):
                tree(sub, depth + 1)

        for g in net.groups:
            if net.parent_of(g) is None:
                tree(g, 0)
        if not net.groups:
            self.log_msg("[dim]no groups — try: group fab1 s1 s2 s3[/]")

    @command("uncluster")
    def cmd_uncluster(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args or args[0].lower() not in net.clusters:
            raise NetError("usage: uncluster <cluster>  (the routers stay grouped)")
        del net.clusters[args[0].lower()]
        self.log_msg(f"{args[0].lower()} is a plain group again")
        self.changed()

    @command("clusters")
    def cmd_clusters(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        for c in net.clusters:
            st = net.ha_state(c)
            self.log_msg(f"[{HA_COLOR[st['level']]}]{c:<8}[/] {' '.join(st['members'])}  {st['text']}")
        if not net.clusters:
            self.log_msg("[dim]no clusters: try cluster rfab1 r1 r2[/]")

    def _members(self, toks: list[str]) -> list[str]:
        """Group-command tokens: existing group names stay names, the rest are device ids."""
        return [t.lower() if t.lower() in self.net.groups else self._ref(t)[0] for t in toks]

    def _group_text(self, g: str) -> str:
        """Members, with subgroups shown as name[device count]."""
        net = self.net
        return " ".join(f"[{group_color(m)}]{m}[/]\\[{len(net.devices_in(m))}]" if m in net.groups else m
                        for m in net.groups[g])

    @command("cluster")
    def cmd_cluster(self, args: list[str], opts: dict[str, str]) -> None:
        net = self.net
        if not args:
            self.dispatch(["clusters"])
            return
        name = args[0].lower()
        policy = opts.get("policy")
        if policy is not None and policy not in POLICIES:
            raise NetError(f"policy must be one of: {', '.join(POLICIES)}")
        if len(args) > 1:
            ids = [net.get(self._ref(t)[0]).id for t in args[1:]]
            bad = [d for d in ids if not net.is_router(d)]
            if bad:
                raise NetError(f"only routers and gateways can be clustered: {' '.join(bad)}")
            net.group_add(name, ids)
            net.clusters[name] = policy or net.clusters.get(name, "none")
            self.log_msg(f"[{group_color(name)}]▣ cluster {name}[/]: {' '.join(net.groups[name])} "
                         f"[dim](policy {net.clusters[name]}; virtual gateway host {VIP_HOST} on each network)[/]")
        elif name not in net.clusters:
            raise NetError(f"no such cluster: {name} (create: cluster {name} r1 r2)")
        elif policy:
            net.clusters[name] = policy
            self.log_msg(f"{name}: split policy → {policy}")
        else:
            st = net.ha_state(name)
            self.log_msg(f"[{HA_COLOR[st['level']]}]{name}: {st['text']}[/]")
            for netid, owners in sorted(st["owners"].items()):
                self.log_msg(f"  {fmt((netid, VIP_HOST))} → {' and '.join(o[0] for o in owners)}")
            return
        self.changed()

    def _ha_report(self) -> None:
        """Log cluster state changes (failover, split-brain, recovery) as they happen."""
        seen = getattr(self, "_ha_seen", {})
        now = {}
        for c in self.net.clusters:
            st = self.net.ha_state(c)
            owners = " ".join(f"{fmt((n, VIP_HOST))}→{'+'.join(o[0] for o in os)}"
                              for n, os in sorted(st["owners"].items()))
            now[c] = (st["level"], st["text"], owners)
            if seen.get(c) != now[c]:
                icon = {"ok": "✓", "degraded": "▲", "split": "⚡", "down": "✕"}[st["level"]]
                self.log_msg(f"[{HA_COLOR[st['level']]}]{icon} {c}: {st['text']}[/]"
                             + (f"\n  [dim]virtual gateways: {owners}[/]" if owners else ""))
        self._ha_seen = now
