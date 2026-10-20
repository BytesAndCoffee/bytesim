"""The inspector panel: a text summary of the selection (Rich markup)."""
from __future__ import annotations

from ..constants import KINDS, VIP_HOST
from ..model import Iface, Network, fmt
from .theme import HA_COLOR, ROLE_COLOR, group_color


def mode_text(ifc: Iface) -> str:
    """How an interface got its address: dhcp, adhoc, static, auto, or dhcp→adhoc."""
    if ifc.mode == "dhcp" and ifc.via == "adhoc":
        return "dhcp→adhoc"
    return ifc.via or ifc.mode


def render_inspector(net: Network, sel: str | None, selected_group: str | None,
                     graph: bool, auto: bool, naming: str = "off", traffic=None,
                     paused: bool = False, queued: int = 0, preparing: bool = False) -> str:
    out = [f"mode: [b]{'graph' if graph else 'addr'}[/]  ·  auto-arrange: "
           f"{'[green]on[/]' if auto else '[yellow]off[/]'}",
           f"layout: [cyan]{net.layout_mode}[/]",
           f"orientation: [cyan]{net.group_orientation}[/]",
           f"playback: {'[yellow]paused[/]' if paused else '[green]running[/]'} · {queued} queued",
           f"auto-renumber: {'[dim]off[/]' if naming == 'off' else f'[cyan]{naming}[/]'}",
           f"{len(net.devices)} devices · {len(net.links)} cables", ""]
    if preparing:
        out.insert(4, "[yellow]preparing packet batch…[/]")
    if traffic is not None:
        st = traffic.stats
        out.insert(-1, f"traffic [green]on[/] {traffic.rate:g}/s · {st.delivered} delivered · "
                       f"{sum(st.dropped.values())} dropped")
    if net.storming:
        out[-1:] = [f"[b red]⚡ broadcast storm:[/] [red]{' '.join(sorted(net.storming, key=net.bridge_id))}[/]", ""]
    for k, (name, _, color) in KINDS.items():
        ids = [d.id for d in net.sorted_devices() if d.kind == k]
        out.append(f"[{color}]■ {name}{'es' if name.endswith('h') else 's'}[/] " + (" ".join(ids) if ids else "[dim]—[/]"))
    def nest(g: str) -> str:
        subs = net.subgroups(g)
        return f"[{group_color(g)}]{g}[/]" + (" ‹" + " ".join(nest(x) for x in subs) + "›" if subs else "")

    tops = [g for g in net.groups if net.parent_of(g) is None]
    out.append("[b]┆ groups[/] " + (" ".join(nest(g) for g in tops) if tops else "[dim]—[/]"))
    out.append("")
    gsel = selected_group
    if gsel and gsel in net.groups:
        members = net.devices_in(gsel)
        inside = sum(1 for l in net.links if l.a in members and l.b in members)
        outside = sum(1 for l in net.links if (l.a in members) != (l.b in members))
        out.append(f"[b reverse {group_color(gsel)}] {gsel} [/] [b]group[/]  {len(members)} devices")
        out.append(f"layout: {net.group_layouts.get(gsel, net.group_orientation)}")
        if (chain := net.chain(gsel)):
            out.append(f"[dim]inside {' › '.join(chain)}[/]")
        for sub in net.subgroups(gsel):
            out.append(f" [{group_color(sub)}]▣ {sub:<10}[/] {len(net.devices_in(sub))} devices")
        out.append(f"[dim]{inside} internal · {outside} external cables[/]")
        if gsel in net.mlag_domains:
            peers = sum(1 for l in net.links if net.is_peer_link(l) and net.end(l.a) == gsel)
            ends = sorted((next(iter(p - {gsel})) for p in net.lags if gsel in p), key=net._end_order)
            out.append(f"[#af87ff]MLAG domain[/] · {peers} peer link{'s' if peers != 1 else ''}"
                       f" · bundles to {' '.join(ends) or '—'}")
        if gsel in net.clusters and not graph:
            st = net.ha_state(gsel)
            out.append(f"[b]HA cluster[/] · policy {st['policy']} · {st['mode']}")
            if st["mode"] == "active-passive":
                out.append(f"forwarding: {' '.join(sorted(st['forwarders'])) or '[red]nobody[/]'}"
                           + (f" · standby {' '.join(st['standby'])}" if st["standby"] else ""))
            out.append(f"[{HA_COLOR[st['level']]}]{st['text']}[/]")
            out.append(f"control links {st['ctrl_up']}/{st['ctrl']} up · "
                       f"{len(st['partitions'])} partition{'s' if len(st['partitions']) != 1 else ''}")
            for netid, owners in sorted(st["owners"].items()):
                out.append(f" {fmt((netid, VIP_HOST))} → {' + '.join(o[0] for o in owners)}")
        for did in [m for m in net.groups[gsel] if m in net.devices]:
            d = net.devices[did]
            used = sum(1 for l in net.links if l.touches(did))
            out.append(f" [{d.color}]■ {did:<5}[/] {d.name:<8} {used}/{d.nports}"
                       + ("  [red]✕ down[/]" if not d.up else ""))
    elif sel and sel in net.devices:
        d = net.devices[sel]
        used = sum(1 for l in net.links if l.touches(d.id))
        kind = "NICs" if d.kind == "c" else "ports"
        chain = net.chain(d.id)
        g = chain[-1] if chain else None
        out.append(f"[b reverse {d.color}] {d.id} [/] [b]{d.name}[/]  {used}/{d.nports} {kind}"
                   + ("  [b red]✕ DOWN[/]" if not d.up else ""))
        if traffic is not None and any(c[d.id] for c in (traffic.stats.talkers, traffic.stats.received,
                                                          traffic.stats.forwarded, traffic.stats.drops_at)):
            st = traffic.stats
            out.append(f"[#87d7ff]traffic[/] sent {st.talkers[d.id]} · received {st.received[d.id]}"
                       f" · forwarded {st.forwarded[d.id]} · dropped {st.drops_at[d.id]}")
        out.append(f"[dim]position {d.x},{d.y}[/]"
                   + (f"  [{group_color(g)}]{' › '.join(chain)}[/]" if g else ""))
        if d.kind in "rg" and not graph:
            out.append("DHCP server " + ("[red]off[/]" if d.id in net.dhcp_off else "[green]on[/]"))
        if d.kind == "g":
            out.append("[magenta]☁ uplink to the internet[/]")
        for p in range(1, d.nports + 1):
            l = net.link_on(d.id, p)
            if l:
                o, op = l.other(d.id)
                od = net.devices[o]
                tag = f" [{ROLE_COLOR[l.role]}]{l.role}[/]" if l.role != "data" else ""
                if net.bundle(l):
                    tag += " [#5fd7ff]≡ lag[/]"
                elif net.is_peer_link(l):
                    tag += " [#af87ff]⇋ peer[/]"
                mark = "[b red]✕[/]" if not l.up else "[b]●[/]"
                if d.kind == "s" and (role := net.port_role(d.id, p)):
                    tag += {"blocking": " [b yellow]⊘ blocking[/]", "root": " [cyan]root[/]",
                            "designated": " [dim]desig[/]", "edge": " [dim]edge[/]"}[role]
                if (d.id, p) in net.stp_blocked:
                    mark = "[b yellow]⊘[/]"
                out.append(f" {mark} {d.port_name(p):<5} → [{od.color}]{o}[/]:{od.port_name(op)}{tag}")
            else:
                out.append(f" [#6c6c6c]○ {d.port_name(p):<5}   free[/]")
            ifc = net.ifaces.get(f"{d.id}:{p}")
            line = f"   [dim]{d.mac(p):<6}[/]"
            if d.kind != "s" and ifc and not graph and (ifc.addr or ifc.mode in ("static", "none")):
                mode = mode_text(ifc)
                color = {"dhcp": "#ffd700", "static": "cyan", "auto": "#9e9e9e", "none": "red"}.get(mode, "yellow")
                line += f" {fmt(ifc.addr)} [{color}]{mode}[/]"
                if ifc.gw is not None:
                    line += f" [dim]gw {ifc.gw}[/]"
            out.append(line)
        if d.kind == "s":
            stp = (f"STP [green]on[/] · root bridge [cyan]{net.stp_roots.get(d.id, d.id)}[/]"
                   if net.stp_enabled(d.id) else "STP [yellow]off[/]")
            prio = net.stp_priority.get(d.id)
            out.append(stp + (f" · priority {prio}" if prio is not None else ""))
            if d.id in net.storming:
                out.append("[b red]⚡ STORMING: every port saturated[/]")
            for mac, ports in sorted(net.mac_flaps.get(d.id, {}).items()):
                out.append(f"[#ffaf00]MAC {mac} flapping {'⇄'.join(f'p{q}' for q in sorted(ports))}[/]")
            table = net.mac_tables.get(d.id, {})
            out.append("")
            out.append(f"[b]MAC table[/] [dim]({len(table)} learned)[/]")
            for mac, port in sorted(table.items(), key=lambda t: (t[1], t[0])):
                out.append(f" {d.port_name(port):<4} {mac}")
            if not table:
                out.append(" [dim]empty: send some traffic[/]")
    else:
        out.append("[dim]nothing selected[/]")
        out.append("[dim]tab / click / type an id or group[/]")
    return "\n".join(out)
