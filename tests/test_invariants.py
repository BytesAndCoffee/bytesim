"""Randomized fault sequences: every packet ByteSim delivers must have a valid hop-by-hop
trace, whatever failed, recovered or aged out before it was sent.

Each case loads a demo, then repeatedly fails or restores a random device or cable (or
lets time pass) and reconciles, as the app does, then plans random traffic. No send,
delivered or not, may leave one public NAT endpoint mapped to two flows, or open a
mapping at a gateway its route never reached. Whatever gets delivered is checked against invariants that don't trust the path search: live
devices and cables, physical continuity, switch-only transit inside a hop, the MAC
tables that hop taught, one network per hop, each router's own table choosing the next
hop, the TTL, and NAT mappings for anything that comes in from a WAN.
"""
import random

import pytest

from bytesim import NetError, demos
from bytesim.constants import TTL
from bytesim.model.routing import DEFAULT
from bytesim.scripting import ScriptRunner


def check_leg(net, a, b, path):
    assert path[0][0] == a[0] and path[-1][0] == b[0], "a hop starts and ends at its interfaces"
    for (u, _), (v, li) in zip(path, path[1:]):
        link = net.links[li]
        assert {link.a, link.b} == {u, v}, f"cable {li} doesn't join {u} and {v}"
        assert net.usable(link), f"cable {li} ({u}–{v}) can't carry data"
        assert net.devices[u].up and net.devices[v].up, f"{u} or {v} is down"
    transit = [d for d, _ in path[1:-1]]
    assert all(net.devices[d].kind == "s" for d in transit), f"a hop forwarded through {transit}"
    mac = net.devices[b[0]].mac(b[1])
    for (sw, _), (_, li) in zip(path[1:-1], path[2:]):
        entry = net.mac_tables.get(sw, {}).get(mac)
        held = net.link_on(sw, entry) if entry is not None else None
        if held is not None and (not net.usable(held) or net.is_peer_link(held)):
            entry = None                    # forgotten (link down), or MLAG: the domain's shared table decides
        if entry is not None:
            out = net.links[li].port_of(sw)
            assert entry in net.lag_ports(sw, out), f"{sw} sent {mac} out p{out}, its table says p{entry}"
    na, nb = net.addr_of(a), net.addr_of(b)
    assert na is not None and nb is not None and na[0] == nb[0], f"hop {a}→{b} spans networks {na} {nb}"


def check_route(net, legs, dst_iface, internet):
    # Until a NAT return reaches the public endpoint it's addressed to, that endpoint is
    # its destination; after the gateway translates it, the next one (or the host).
    endpoints = {m["public"][0] for maps in net.nat_table.values() for m in maps if m["expires"] > net.clock}
    for i in range(len(legs) - 1):
        router = legs[i][1][0]
        assert legs[i + 1][0][0] == router, "consecutive hops meet at one router"
        assert net.is_router(router), f"{router} forwarded but isn't a router"
        nxt = legs[i + 1][1]
        ahead = [b for _, b, _ in legs[i + 1:] if net.addr_of(b) in endpoints and b[0] != router]
        target = ahead[0] if ahead else (None if internet else dst_iface)
        if nxt == target:
            continue                                    # delivered on a connected network
        table = net.routing_table(router)
        key = DEFAULT if target is None else net.addr_of(target)[0]
        route = table.get(key) or table.get(DEFAULT)
        assert route is not None and any(e == nxt for _, e in route.hops), \
            f"{router} sent toward {nxt}, not a next hop in its table for {key}"
    assert len(legs) - 1 < TTL, "more routers than the TTL allows"
    for (_, (r, p_in), _), ((_, p_out), _, _) in zip(legs, legs[1:]):
        if net.natting(r) and p_in in net.wan_ports(r) and p_out not in net.wan_ports(r):
            inside = net.addr_of(legs[-1][1])
            live = any(m["expires"] > net.clock for m in net.nat_table.get(r, []))
            assert live, f"WAN→LAN through {r}'s NAT to {inside} without a live mapping"


def check_nat_endpoints(net):
    """No gateway maps one live public endpoint to two inside flows."""
    for g, maps in net.nat_table.items():
        held: dict = {}
        for m in maps:
            if m["expires"] > net.clock:
                held.setdefault(m["public"], set()).add(m["inside"])
        assert all(len(v) == 1 for v in held.values()), f"{g} maps one public endpoint to several flows: {held}"


def step(net, r, rng):
    roll = rng.random()
    if roll < 0.15:
        r.run(f"wait {rng.choice([5, 30, 61, 301, 1801])}")
        return
    if roll < 0.55 and net.links:
        l = rng.choice(net.links)
        l.up = not l.up if rng.random() < 0.6 else True
    else:
        d = rng.choice([d for d in net.devices.values() if d.kind != "c"] or list(net.devices.values()))
        if d.up:
            r.run(f"fail {d.id}")
        else:
            r.run(f"restore {d.id}")
    net.reconcile()


@pytest.mark.parametrize("demo", ["basic", "core", "ha", "mlag", "internet", "overlay"])
@pytest.mark.parametrize("seed", range(20))
def test_every_delivered_packet_has_a_valid_trace(demo, seed):
    rng = random.Random(f"{demo}-{seed}")
    net, _ = demos.DEMOS[demo]()
    net.reconcile()
    r = ScriptRunner(net, detail=False)
    hosts = sorted(d for d in net.devices if net.devices[d].kind == "c")
    checked = 0
    for _ in range(12):
        step(net, r, rng)
        for _ in range(4):
            src = rng.choice(hosts)
            dst = rng.choice([h for h in hosts if h != src] + ["internet"])
            before = {g: len(maps) for g, maps in net.nat_table.items()}
            try:
                _, _, legs, internet = r._plan(src, dst)
            except NetError:
                check_nat_endpoints(net)
                target = None if dst == "internet" else (dst, 1)
                reached = {b[0] for _, b, _ in net.trace_route(src, target)[0]}    # as far as routing goes
                grew = {g for g, maps in net.nat_table.items() if len(maps) > before.get(g, 0)}
                assert grew <= reached, f"a failed send opened NAT mappings at {grew - reached}, never reached"
                continue                                 # not delivered: nothing more to vouch for
            check_nat_endpoints(net)
            if not legs:
                continue
            for a, b, path in legs:
                check_leg(net, a, b, path)
            if not r._carried:
                check_route(net, legs, legs[-1][1], internet)
            checked += 1
    assert checked, "the sequence should deliver something to check"


def test_the_checks_catch_an_impossible_trace():
    net, _ = demos.DEMOS["basic"]()
    net.reconcile()
    r = ScriptRunner(net, detail=False)
    _, _, legs, internet = r._plan("c1", "c6")
    a, b, path = legs[0]
    net.links[path[1][1]].up = False                  # the first cable dies under the trace
    with pytest.raises(AssertionError, match="can't carry data"):
        check_leg(net, a, b, path)
    net.links[path[1][1]].up = True
    bogus = [legs[0], (legs[1][0], ("c2", 1), legs[1][2])] + legs[2:]
    with pytest.raises(AssertionError):
        check_leg(net, *bogus[1])
