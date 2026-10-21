"""Canonical form of a network, and the differences between two of them.

Two networks are *equivalent* when they agree on everything ByteSim promises a
construction script rebuilds: device ids, kinds and port counts; id counters
(so the next new device gets the same id); groups, their nesting and member
order; every cable's endpoints, ports and role; HA clusters; STP, DHCP-server
and interface settings (STP port costs among them); static routes; and which
devices and cables are failed.

Deliberately ignored: positions and display layout settings, learned addresses
(re-learned by DHCP and router autoconfiguration), MAC tables, STP's blocked
ports (recomputed), storms, and the order groups were created in.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..constants import KINDS
from .network import Network

DEFAULT_MODE = {"c": "dhcp", "g": "auto", "r": "auto"}


def iface_config(net: Network, dev_id: str, port: int) -> tuple[str, Any]:
    """An interface's configuration: (mode, static address or None)."""
    dev = net.devices[dev_id]
    ifc = net.ifaces.get(f"{dev_id}:{port}")
    mode = ifc.mode if ifc else DEFAULT_MODE[dev.kind]
    static = (ifc.net, ifc.host, ifc.gw) if ifc and mode == "static" and ifc.addr else None
    return mode, static


def link_key(a: str, pa: int, b: str, pb: int, role: str) -> tuple:
    return (*sorted([(a, pa), (b, pb)], key=lambda e: (e[0][0], int(e[0][1:]), e[1])), role)


def canonical(net: Network) -> dict[str, Any]:
    nums = {k: [d.num for d in net.devices.values() if d.kind == k] for k in KINDS}
    ifaces = {}
    for d in net.sorted_devices():
        if d.kind == "s":
            continue
        for p in range(1, d.nports + 1):
            cfg = iface_config(net, d.id, p)
            if cfg != (DEFAULT_MODE[d.kind], None):
                ifaces[f"{d.id}:{p}"] = cfg
    return {
        "devices": {d.id: (d.kind, d.nports) for d in net.devices.values()},
        "counters": {k: max([net.counters[k], *nums[k]]) for k in KINDS},
        "cables": Counter(link_key(l.a, l.pa, l.b, l.pb, l.role) for l in net.links),
        "groups": {g: tuple(ms) for g, ms in net.groups.items()},
        "clusters": dict(net.clusters),
        "cluster_modes": dict(net.cluster_modes),
        "stp_off": frozenset(net.stp_off),
        "stp_priority": dict(net.stp_priority),
        "stp_cost": dict(net.stp_cost),
        "static_routes": {r: dict(routes) for r, routes in net.static_routes.items()},
        "dhcp_off": frozenset(net.dhcp_off),
        "lags": frozenset(net.lags),
        "mlag_domains": frozenset(net.mlag_domains),
        "internet": net.internet,
        "nat_off": frozenset(net.nat_off),
        "nat_hard": frozenset(net.nat_hard),
        "overlays": {t: (v["net"], frozenset(v["hosts"].items()), v.get("coord"), v.get("exit"), v.get("lan", False),
                         tuple(v.get("routes", [])),        # order decides which router wins for a network
                         frozenset(map(tuple, v["acl"])) if "acl" in v else None)
                     for t, v in net.overlay_state().items()},
        "relays": tuple(net.relays),
        "ifaces": ifaces,
        "devices_down": frozenset(d.id for d in net.devices.values() if not d.up),
        "cables_down": Counter(link_key(l.a, l.pa, l.b, l.pb, l.role) for l in net.links if not l.up),
    }


def differences(want: Network, got: Network, limit: int = 8) -> list[str]:
    """Human-readable differences between two networks' canonical forms ([] = equivalent)."""
    a, b = canonical(want), canonical(got)
    out: list[str] = []
    for key in a:
        x, y = a[key], b[key]
        if x == y:
            continue
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y), key=str):
                if x.get(k) != y.get(k):
                    out.append(f"{key} {k}: expected {x.get(k)!r}, got {y.get(k)!r}")
        elif isinstance(x, Counter):
            for k in sorted((x - y).keys()):
                out.append(f"{key}: missing {_cable(k)}")
            for k in sorted((y - x).keys()):
                out.append(f"{key}: unexpected {_cable(k)}")
        else:
            out.append(f"{key}: expected {sorted(x)}, got {sorted(y)}")
    return out[:limit] + ([f"… and {len(out) - limit} more"] if len(out) > limit else [])


def _cable(key: tuple) -> str:
    (a, pa), (b, pb), role = key
    return f"{a}:p{pa}–{b}:p{pb}" + (f" ({role})" if role != "data" else "")
