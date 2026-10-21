"""Forwarding fidelity: echo replies route on their own, TTLs run out, standbys rest,
source interfaces bind, and switches forward by the MAC tables ARP teaches them."""
import asyncio

import pytest
from rich.text import Text

from bytesim import NetError, Network
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp


def build(kinds, cables):
    net = Network()
    for k in kinds:
        net.add(*k) if isinstance(k, tuple) else net.add(k)
    for a, b in cables:
        net.connect(a, None, b, None)
    net.reconcile()
    return net


def plain(r):
    return "\n".join(Text.from_markup(m).plain for m in r.log)


def routed():
    """c1 -- r1 -- r2 -- c2, with c2 statically addressed and no gateway."""
    net = build("crrc", [("c1", "r1"), ("r1", "r2"), ("r2", "c2")])
    r = ScriptRunner(net)
    r.run("ip c2 (3,10)")
    return r


def chain(routers):
    ids = ["c1", *[f"r{i}" for i in range(1, routers + 1)], "c2"]
    return ScriptRunner(build("c" + "r" * routers + "c", list(zip(ids, ids[1:]))))


def lan():
    """c1, c2 and c3 on one switch (ports 1, 2, 3)."""
    return build(["c", ("s", 4), "c", "c"], [("c1", "s1"), ("s1", "c2"), ("s1", "c3")])


# ---- echo replies --------------------------------------------------------------

def test_ping_reply_needs_its_own_return_route():
    r = routed()
    with pytest.raises(NetError, match="c2 has no gateway"):
        r.net.route("c2", ("c1", 1))
    r.run("ping c1 c2")
    text = plain(r)
    assert "  reply from c2" not in text
    assert "✗ no reply from c2: network 1 isn't local to c2 and c2 has no gateway" in text


def test_ping_reply_trace_reports_the_routed_reply_or_its_failure():
    r = routed()
    r.run("trace on\nping c1 c2")
    assert "TRACE REPLY FAILED c2 → c1" in plain(r)
    r.run("ip c2 (3,10) 1")
    r.log.clear()
    r.run("ping c1 c2")
    text = plain(r)
    assert "TRACE REPLY c2 → c1" in text and "reply from c2: hops=3 ttl=62" in text


def test_the_real_app_reports_no_reply_without_a_return_route():
    async def go():
        r = routed()
        app = ByteSimApp(r.net, auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("ping c1 c2")
            for _ in range(1000):
                if not app.canvas.busy:
                    break
                app.canvas._tick()
            visible = " ".join(line.text for line in app.query_one("#log").lines)
            assert "✗ no reply from c2" in visible and "  reply from c2" not in visible

    asyncio.run(go())


# ---- TTL ---------------------------------------------------------------------

def test_ttl_reaches_one_after_63_routers():
    r = chain(63)
    r.run("ping c1 c2")
    assert "reply from c2: hops=64 ttl=1" in plain(r)


def test_ttl_expires_at_the_64th_router():
    r = chain(64)
    with pytest.raises(NetError, match="TTL expired at r64, the 64th router"):
        r.run("ping c1 c2")
    with pytest.raises(NetError, match="TTL expired at r64"):
        r.run("unicast c1 c2")


# ---- active-passive standby ----------------------------------------------------

def standby_pair():
    net = build(["c", "s", ("r", 3), ("r", 3), "s", "c"],
                [("c1", "s1"), ("s1", "r1"), ("s1", "r2"), ("r1", "s2"), ("r2", "s2"), ("s2", "c2")])
    r = ScriptRunner(net)
    r.run("r1:3 r2:3 role=control\ncluster ha r1 r2 mode=active-passive")
    net.reconcile()
    assert net.standby_routers() == {"r2"}
    return r


def test_a_standby_does_not_forward_as_an_explicit_gateway():
    r = standby_pair()
    r.run("ip c1 (1,10) 2")                              # r2's own address on the LAN
    with pytest.raises(NetError, match="gateway \\(1,2\\) is r2, an active-passive standby"):
        r.net.route("c1", ("c2", 1))
    r.run("ip c1 (1,10) 1")                              # the active member forwards
    assert [leg[1][0] for leg in r.net.route("c1", ("c2", 1))] == ["r1", "c2"]


def test_a_standby_still_answers_for_itself():
    r = standby_pair()
    r.run("ip c1 (1,10) 2")
    assert r.net.route("c1", ("r2", 1))[-1][1] == ("r2", 1)


# ---- source interface ------------------------------------------------------------

def two_nics():
    """c1:1 to c2 (network 1), c1:2 to c3 (network 2)."""
    return ScriptRunner(build([("c", 2), "c", "c"], [("c1", "c2"), ("c1", "c3")]))


def test_a_source_port_binds_the_outgoing_interface():
    r = two_nics()
    with pytest.raises(NetError, match="network 2 isn't local to c1:nic1"):
        r.run("unicast c1:1 c3")
    r.run("trace on\nunicast c1:2 c3")
    assert "endpoints c1:nic2" in plain(r)
    r.run("ping c1:1 c2")
    assert "reply from c2" in plain(r)


def test_unusable_source_ports_are_rejected():
    r = two_nics()
    with pytest.raises(NetError, match="c1 has no port 5"):
        r.run("ping c1:5 c2")
    net = build(["c", "r", "c"], [("c1", "r1"), ("r1", "c2")])
    with pytest.raises(NetError, match="r1 is a router: it picks its own outgoing interface"):
        ScriptRunner(net).run("ping r1:1 c2")
    with pytest.raises(NetError, match="graph mode follows cables"):
        ScriptRunner(two_nics().net).run("mode graph\nping c1:1 c2")


# ---- MAC tables ------------------------------------------------------------------

def test_frames_follow_the_mac_table():
    net = lan()
    assert net.forward(("c1", 1), ("c2", 1)) == ([("c1", -1), ("s1", 0), ("c2", 1)], "")   # unknown: flooded
    net.mac_tables = {"s1": {"C2A": 3}}
    path, why = net.forward(("c1", 1), ("c2", 1))
    assert path is None and "reached c3 instead" in why
    net.mac_tables = {"s1": {"C2A": 1}}
    path, why = net.forward(("c1", 1), ("c2", 1))
    assert path is None and "where the frame came in" in why


def test_the_arp_reply_corrects_a_wrong_entry_before_the_frame():
    net = lan()
    r = ScriptRunner(net)
    net.mac_tables = {"s1": {"C2A": 3}}
    r.run("unicast c1 c2")
    assert "delivered to c2" in plain(r)
    assert net.mac_tables["s1"] == {"C1A": 1, "C2A": 2}


def test_an_entry_on_a_dead_cable_is_forgotten():
    net = lan()
    net.mac_tables = {"s1": {"C2A": 3}}
    net.links[2].up = False                              # s1 -- c3
    assert net.forward(("c1", 1), ("c2", 1))[0] == [("c1", -1), ("s1", 0), ("c2", 1)]


def test_arp_requests_teach_every_switch_they_flood():
    net = build(["c", "s", "c", "s", "c"], [("c1", "s1"), ("s1", "c2"), ("s1", "s2"), ("s2", "c3")])
    ScriptRunner(net).run("unicast c1 c2")
    assert net.mac_tables == {"s1": {"C1A": 1, "C2A": 2}, "s2": {"C1A": 1}}


def test_a_routed_ping_teaches_each_segment_both_ends_of_its_hop():
    net = build(["c", "s", "r", "s", "c"], [("c1", "s1"), ("s1", "r1"), ("r1", "s2"), ("s2", "c2")])
    ScriptRunner(net).run("ping c1 c2")
    assert net.mac_tables == {"s1": {"C1A": 1, "R1A": 2}, "s2": {"R1B": 1, "C2A": 2}}


# ---- ARP for an address nobody has ---------------------------------------------------

def loop(stp=True):
    net = build(["c", "s", "s", "s", "c"],
                [("c1", "s1"), ("s1", "s2"), ("s2", "s3"), ("s3", "s1"), ("s3", "c2")])
    r = ScriptRunner(net)
    if not stp:
        r.run("stp off")
        net.reconcile()
    return r


def test_arp_for_a_missing_address_floods_the_lan():
    r = loop()
    with pytest.raises(NetError, match="nobody has \\(1,99\\) \\(ARP from c1:nic1 got no reply\\)"):
        r.run("ping c1 (1,99)")
    assert all(t.get("C1A") for t in r.net.mac_tables.values()) and len(r.net.mac_tables) == 3


def test_arp_for_a_missing_address_sets_off_a_storm_in_a_loop():
    r = loop(stp=False)
    with pytest.raises(NetError, match="its broadcast set off a storm"):
        r.run("ping c1 (1,99)")
    assert r.net.storming == {"s1", "s2", "s3"}


def test_a_router_asks_for_a_missing_address_on_its_own_network():
    r = ScriptRunner(build("crrc", [("c1", "r1"), ("r1", "r2"), ("r2", "c2")]))
    with pytest.raises(NetError, match="nobody has \\(3,99\\) \\(ARP from r2:p2 got no reply\\)"):
        r.run("ping c1 (3,99)")
    with pytest.raises(NetError, match="nobody has \\(9,99\\): no route to network 9"):
        r.run("ping c1 (9,99)")


# ---- link aggregation ------------------------------------------------------------

def bundle(members=3):
    net = build([("s", 8), ("s", 8)], [("s1", "s2")] * members)
    ScriptRunner(net).run("lag s1 s2")
    net.reconcile()
    return net


def test_a_flow_sticks_to_one_member_and_flows_spread_over_all():
    net = bundle()
    flows = [f"c{i}>c{i + 1}" for i in range(200)]
    picks = {f: net._logical_link("s1", 1, f) for f in flows}
    assert all(net._logical_link("s1", 1, f) == picks[f] for f in flows)
    assert set(picks.values()) == {0, 1, 2}
    assert min(list(picks.values()).count(i) for i in range(3)) > 40


def test_a_member_failure_moves_only_its_own_flows():
    net = bundle()
    flows = [f"c{i}>c{i + 1}" for i in range(200)]
    before = {f: net._logical_link("s1", 1, f) for f in flows}
    net.links[1].up = False
    after = {f: net._logical_link("s1", 1, f) for f in flows}
    assert all(after[f] == before[f] for f in flows if before[f] != 1)
    assert {after[f] for f in flows if before[f] == 1} == {0, 2}
    net.links[1].up = True
    assert {f: net._logical_link("s1", 1, f) for f in flows} == before


# ---- MAC aging, flushing and moves ------------------------------------------------

def test_mac_entries_age_out_and_unknown_frames_flood_again():
    net = lan()
    r = ScriptRunner(net)
    r.run("unicast c1 c2")
    assert net.forward(("c1", 1), ("c2", 1))[0] and net.flooded == []
    r.run("wait 299")
    assert net.mac_tables["s1"]["C2A"] == 2
    r.run("wait 1s")
    assert "s1" not in net.mac_tables
    assert net.forward(("c1", 1), ("c2", 1))[0] == [("c1", -1), ("s1", 0), ("c2", 1)]
    assert net.flooded == ["s1"]
    assert "clock: 0:05:00" in plain(r)


def test_wait_rejects_nonsense():
    r = ScriptRunner(lan())
    for bad in ("wait -1", "wait soon", "wait 8d", "wait 1 2"):
        with pytest.raises(NetError):
            r.run(bad)


def test_a_dead_link_flushes_only_its_port():
    net = lan()
    r = ScriptRunner(net)
    r.run("unicast c1 c2\nunicast c3 c1")
    r.run("fail s1:3")
    assert net.mac_tables["s1"] == {"C1A": 1, "C2A": 2}
    r.run("restore s1:3")
    assert "C3A" not in net.mac_tables["s1"]


def test_a_moved_host_announces_its_new_port():
    net = lan()
    r = ScriptRunner(net)
    r.run("unicast c1 c2")
    assert net.mac_tables["s1"]["C2A"] == 2
    r.run("unlink c2\nc2 s1:4")
    net.reconcile()
    assert net.mac_tables["s1"]["C2A"] == 4                 # its gratuitous ARP
    last = net.forward(("c1", 1), ("c2", 1))[0][-1]
    assert last[0] == "c2" and net.links[last[1]].port_of("s1") == 4


def test_an_stp_topology_change_flushes_the_area():
    net = build(["c", "s", "s", "s", "c"],
                [("c1", "s1"), ("s1", "s2"), ("s2", "s3"), ("s3", "s1"), ("s3", "c2")])
    r = ScriptRunner(net)
    r.run("unicast c1 c2")
    assert set(net.mac_tables) == {"s1", "s2", "s3"}
    forwarding = next(i for i, l in enumerate(net.links) if {l.a, l.b} == {"s1", "s3"})
    net.links[forwarding].up = False
    net.reconcile()
    assert not net.mac_tables
    r.run("unicast c1 c2")
    assert "delivered to c2" in plain(r)


def test_a_host_plugged_into_a_live_loop_storms_at_once():
    r = loop(stp=False)
    r.net.add("c")
    r.run("c3 s2")
    notes = r.net.reconcile()
    assert any("c3's gratuitous ARP set off a broadcast storm" in n for n in notes)
    assert r.net.storming == {"s1", "s2", "s3"}


# ---- ARP caches -------------------------------------------------------------------

def counting_floods(net):
    calls = []
    real = net.flood

    def flood(src):
        calls.append(src)
        return real(src)
    net.flood = flood
    return calls


def test_the_first_send_asks_and_the_second_uses_the_cache():
    net = lan()
    r = ScriptRunner(net)
    floods = counting_floods(net)
    r.run("unicast c1 c2")
    assert floods == [("c1", 1)] and "asked by c1:nic1" in plain(r)
    assert net.arp_lookup(("c1", 1), (1, 11)) == ("c2", 1)
    assert net.arp_lookup(("c2", 1), (1, 10)) == ("c1", 1)       # the target learned the asker
    r.log.clear()
    r.run("unicast c1 c2\nunicast c2 c1")
    assert floods == [("c1", 1)] and "cached by c1:nic1" in plain(r)


def test_entries_expire_and_the_next_send_asks_again():
    net = lan()
    r = ScriptRunner(net)
    floods = counting_floods(net)
    r.run("unicast c1 c2\nwait 59\nunicast c1 c2")
    assert len(floods) == 1
    r.run("wait 60\nunicast c1 c2")
    assert len(floods) == 2


def test_arp_shows_and_clears_caches():
    r = ScriptRunner(lan())
    r.run("unicast c1 c2\narp c1")
    assert "c1:nic1    (1,11)    C2A     in 60s" in plain(r)
    r.run("arp c1 clear")
    assert r.net.arp_lookup(("c1", 1), (1, 11)) is None and r.net.arp_lookup(("c2", 1), (1, 10))


def test_a_stale_entry_loses_one_packet_then_resolves_again():
    net = lan()
    r = ScriptRunner(net)
    r.run("ip c2 (1,11)\nip c3 (1,12)\nunicast c1 c2")
    r.run("ip c2 (1,30)\nip c3 (1,11)")                  # c3 takes over c2's old address
    with pytest.raises(NetError, match="went to c2:nic1, which no longer has that address"):
        r.run("unicast c1 (1,11)")
    r.log.clear()
    r.run("unicast c1 (1,11)")
    assert "delivered to (1,11) c3" in plain(r)


def test_a_moved_host_is_found_without_asking_again():
    net = build(["c", "s", "c", "s"], [("c1", "s1"), ("s1", "c2"), ("s1", "s2")])
    r = ScriptRunner(net)
    floods = counting_floods(net)
    r.run("unicast c1 c2")
    r.run("unlink c2\nc2 s2")
    net.reconcile()                                       # c2's gratuitous ARP re-teaches the switches
    r.log.clear()
    asked = len(floods)
    r.run("unicast c1 c2")
    assert "delivered to c2: 3 hops" in plain(r) and len(floods) == asked + 0


def test_caches_follow_a_virtual_gateway_failover():
    net = build(["c", "s", ("r", 3), ("r", 3), "s", "c"],
                [("c1", "s1"), ("s1", "r1"), ("s1", "r2"), ("r1", "s2"), ("r2", "s2"), ("s2", "c2")])
    r = ScriptRunner(net)
    r.run("r1:3 r2:3 role=control\ncluster ha r1 r2")
    net.reconcile()
    r.run("ping c1 c2")
    assert net.arp_lookup(("c1", 1), (1, 254))[0] == "r1"
    r.run("fail r1")
    net.reconcile()
    r.log.clear()
    r.run("ping c1 c2")
    assert "  reply from c2" in plain(r)
    assert net.arp_lookup(("c1", 1), (1, 254))[0] == "r2"


def test_a_failed_device_comes_back_with_empty_caches():
    r = ScriptRunner(lan())
    r.run("unicast c1 c2\nfail c1\nrestore c1")
    assert r.net.arp_lookup(("c1", 1), (1, 11)) is None


# ---- STP path costs ----------------------------------------------------------------

def square():
    """s1 (root) to s4 two ways: s1-s2-s4 and s1-s3-s4."""
    net = build([("s", 4)] * 4 + ["c"], [("s1", "s2"), ("s1", "s3"), ("s2", "s4"), ("s3", "s4"), ("s4", "c1")])
    return net, ScriptRunner(net)


def test_equal_costs_keep_the_lower_bridge_id_and_a_cost_moves_the_root_port():
    net, r = square()
    assert net.stp_roots["s4"] == "s1"
    assert net.stp_root_ports["s4"] == 1 and ("s4", 2) in net.stp_blocked     # via s2
    r.run("stp cost s4:1 50000")
    net.reconcile()
    assert net.stp_root_ports["s4"] == 2 and ("s4", 1) in net.stp_blocked     # now via s3
    assert net.root_cost("s4") == 40000
    r.run("stp")
    assert "s4 40000 (root port p2)" in plain(r) and "s4:p1=50000" in plain(r)


def test_a_cheap_long_way_beats_an_expensive_short_one():
    net = build([("s", 4)] * 3, [("s1", "s2"), ("s1", "s3"), ("s3", "s2")])
    r = ScriptRunner(net)
    r.run("stp cost s2:1 100000")                          # s2's direct port to the root
    net.reconcile()
    assert net.stp_root_ports["s2"] == 2 and ("s2", 1) in net.stp_blocked


def test_disconnected_areas_get_their_own_trees():
    net = build([("s", 4)] * 6, [("s1", "s2"), ("s2", "s3"), ("s3", "s1"),
                                  ("s4", "s5"), ("s5", "s6"), ("s6", "s4")])
    assert net.stp_roots["s3"] == "s1" and net.stp_roots["s6"] == "s4"
    assert len(net.stp_blocked) == 2 and not net.loop_links()


def test_stp_costs_survive_saves_exports_and_topologies():
    from bytesim.model import Network as N
    from bytesim.synthesis import synthesize
    from bytesim.topology import dump_topology, load_topology
    net, r = square()
    r.run("stp cost s4:1 50000")
    assert N.from_json(net.to_json()).stp_cost == {("s4", 1): 50000}
    assert load_topology(dump_topology(net)).stp_cost == {("s4", 1): 50000}
    exported = synthesize(net)
    assert "stp cost s4:1 50000" in exported.script and not exported.limitations
    with pytest.raises(NetError):
        r.run("stp cost s4:9 5")
    r.run("stp cost s4:1 default")
    assert not net.stp_cost


# ---- routing tables ------------------------------------------------------------------

def diamond():
    """c1 - r1 = (r2 | r3) = r4 - c2, plus c3 on r4: two equal ways from r1 to r4."""
    net = build(["c", ("r", 6), ("r", 6), ("r", 6), ("r", 6), "c", "c"],
                [("c1", "r1"), ("r1", "r2"), ("r1", "r3"), ("r2", "r4"), ("r3", "r4"), ("r4", "c2"), ("r4", "c3")])
    return net, ScriptRunner(net)


def via(net, src, dst):
    return [leg[1][0] for leg in net.route(src, (dst, 1))][1:-1]


def test_tables_hold_connected_learned_and_ecmp_routes():
    net, r = diamond()
    table = net.routing_table("r1")
    c2net = net.addr_of(("c2", 1))[0]
    assert table[net.addr_of(("c1", 1))[0]].kind == "connected"
    assert table[c2net].kind == "learned" and table[c2net].cost == 2 and len(table[c2net].hops) == 2
    r.run("routes r1")
    assert f"network {c2net}" in plain(r) and "ECMP" in plain(r)


def test_a_flow_keeps_its_next_hop_and_flows_use_both():
    from bytesim.model.layer2 import flow_choice
    net, _ = diamond()
    assert len({via(net, "c1", "c2")[0] for _ in range(5)}) == 1
    hops = net.routing_table("r1")[net.addr_of(("c2", 1))[0]].hops
    picks = [flow_choice(hops, f"c{i}>c2", "r1")[1][0] for i in range(64)]
    assert set(picks) == {"r2", "r3"} and min(picks.count("r2"), picks.count("r3")) > 16
    lost = [i for i, p in enumerate(picks) if p == "r2"]
    survivors = [flow_choice([h for h in hops if h[1][0] == "r3"], f"c{i}>c2", "r1")[1][0] for i in range(64)]
    assert all(survivors[i] == picks[i] for i in range(64) if i not in lost)


def test_a_static_route_changes_only_the_traffic_that_depends_on_it():
    net, r = diamond()
    c2net, c3net = net.addr_of(("c2", 1))[0], net.addr_of(("c3", 1))[0]
    before = via(net, "c1", "c3")
    other = "r3" if via(net, "c1", "c2")[0] == "r2" else "r2"
    hop = next(net.addr_of(e) for _, e in net.routing_table("r1")[c2net].hops if e[0] == other)
    r.run(f"route r1 {c2net} via ({hop[0]},{hop[1]})")
    assert via(net, "c1", "c2")[0] == other
    assert via(net, "c1", "c3") == before
    assert net.routing_table("r1")[c3net].kind == "learned"


def test_no_route_means_unreachable_even_with_a_physical_path():
    net, r = diamond()
    c2net = net.addr_of(("c2", 1))[0]
    r.run(f"route r1 {c2net} drop")
    with pytest.raises(NetError, match=f"r1 drops traffic for network {c2net}"):
        r.run("ping c1 c2")
    r.run(f"route r1 {c2net} auto")
    r.run("ping c1 c2")
    assert "  reply from c2" in plain(r)


def test_withdrawn_routes_move_and_a_dead_static_next_hop_is_uninstalled():
    net, r = diamond()
    c2net = net.addr_of(("c2", 1))[0]
    first = via(net, "c1", "c2")[0]
    hop = next(net.addr_of(e) for _, e in net.routing_table("r1")[c2net].hops if e[0] == first)
    r.run(f"route r1 {c2net} via ({hop[0]},{hop[1]})")
    r.run(f"fail {first}")
    net.reconcile()
    table = net.routing_table("r1")[c2net]
    assert table.kind == "learned" and len(table.hops) == 1          # the static one isn't installed
    assert via(net, "c1", "c2")[0] != first


def test_a_static_routing_loop_runs_out_of_ttl():
    net, r = diamond()
    c2net = net.addr_of(("c2", 1))[0]
    r1_on_r2 = next(net.addr_of(e) for _, e in net.routing_table("r2")[net.addr_of(("c1", 1))[0]].hops)
    r2_on_r1 = next(net.addr_of(e) for _, e in net.routing_table("r1")[c2net].hops if e[0] == "r2")
    r.run(f"route r1 {c2net} via ({r2_on_r1[0]},{r2_on_r1[1]})\nroute r2 {c2net} via ({r1_on_r2[0]},{r1_on_r2[1]})")
    with pytest.raises(NetError, match="TTL expired"):
        r.run("unicast c1 c2")


def test_a_default_route_reaches_the_gateway():
    net = build(["c", "r", "r", "g"], [("c1", "r1"), ("r1", "r2"), ("r2", "g1")])
    assert net.routing_table("r1")[0].kind == "default"
    assert net.routing_table("g1")[0].kind == "exit"
    assert [leg[1][0] for leg in net.route("c1", None)] == ["r1", "r2", "g1"]


def test_an_on_link_destination_on_another_segment_is_not_sent_to_the_gateway():
    net = build(["c", "r", "r", "c"], [("c1", "r1"), ("r1", "r2"), ("r2", "c2")])
    r = ScriptRunner(net)
    r.run("ip c1 (1,10) 1\nip c2 (1,20)")
    with pytest.raises(NetError, match="on c1's own network 1, but nothing on c1:nic1's link has it"):
        r.run("unicast c1 c2")


def test_static_routes_survive_saves_exports_and_topologies():
    from bytesim.model import Network as N
    from bytesim.synthesis import synthesize
    from bytesim.topology import dump_topology, load_topology
    net, r = diamond()
    r.run("route r1 9 drop\nroute r1 default via (2,2)")
    want = {"r1": {9: "drop", 0: (2, 2)}}
    assert N.from_json(net.to_json()).static_routes == want
    assert load_topology(dump_topology(net)).static_routes == want
    exported = synthesize(net)
    assert "route r1 9 drop" in exported.script and "route r1 default via (2,2)" in exported.script
    assert not exported.limitations


# ---- stateful NAT ---------------------------------------------------------------------

def internet():
    from bytesim import demos
    net, _ = demos.internet()
    net.reconcile()
    return net, ScriptRunner(net, detail=True)


def test_two_inside_hosts_get_distinct_mappings_and_their_own_replies():
    net, r = internet()
    r.run("ping c1 c7\nping c2 c7")
    maps = net.nat_table["g3"]
    assert {m["inside"][0] for m in maps} == {(6, 10), (6, 11)}
    assert len({m["public"] for m in maps}) == 2
    assert {m["public"][0] for m in maps} == {net.addr_of(("g3", p)) for p in net.wan_ports("g3")}
    assert plain(r).count("  reply from c7") == 2
    _, back, legs, _ = r._plan("c7", "(6,11)", reply=True)
    assert legs[-1][1] == ("c2", 1)


def test_the_return_direction_of_a_flow_gets_in_until_its_mapping_expires():
    net, r = internet()
    with pytest.raises(NetError, match="dropped by g3's NAT: unsolicited inbound"):
        r.run("unicast c7 c1")
    r.run("unicast c1 c7\nunicast c7 c1")
    assert "delivered to c1" in plain(r) and "the mapping (6,10)'s flow made" in plain(r)
    r.run("wait 61")
    assert not net.nat_table
    with pytest.raises(NetError, match="unsolicited inbound"):
        r.run("unicast c7 c1")


def test_a_reply_after_the_gateway_restarted_is_dropped():
    net, r = internet()
    r.run("unicast c1 c7")
    r.run("fail g3\nrestore g3")
    net.reconcile()
    assert "g3" not in net.nat_table
    with pytest.raises(NetError, match="unsolicited inbound"):
        r.run("unicast c7 c1")


def test_nat_shows_a_gateways_mappings():
    net, r = internet()
    r.run("ping c1 c7\nnat g3")
    assert "(6,10):49152 ⇄ " in plain(r) and "with c7" in plain(r)


def test_the_outside_has_no_route_into_a_nat_lan():
    net, _ = internet()
    inside = net.addr_of(("c1", 1))[0]
    assert inside not in net.routing_table("g1")
    assert inside in net.routing_table("g1", nat=False)
    assert inside in net.routing_table("g3")


# ---- DHCP leases -------------------------------------------------------------------------

def dhcp_lan(clients=2):
    kinds = ["r", ("s", 8)] + ["c"] * clients
    return build(kinds, [("r1", "s1")] + [("s1", f"c{i}") for i in range(1, clients + 1)])


def test_each_client_gets_a_unique_lease_the_server_remembers():
    net = dhcp_lan(3)
    hosts = [net.iface(f"c{i}", 1).host for i in (1, 2, 3)]
    assert len(set(hosts)) == 3
    binds = net.dhcp_leases["r1:1"]
    assert {b["host"] for b in binds.values()} == set(hosts)
    assert all(net.iface(f"c{i}", 1).lease == 3600 for i in (1, 2, 3))


def test_clients_renew_at_half_time_and_keep_their_address():
    net = dhcp_lan()
    r = ScriptRunner(net)
    before = net.iface("c1", 1).addr
    r.run("wait 1799")
    assert net.iface("c1", 1).lease == 3600
    r.run("wait 1")
    assert "c1:nic1 renewed (1,10) with r1" in plain(r)
    assert net.iface("c1", 1).addr == before and net.iface("c1", 1).lease == 5400
    r.run("wait 10h")
    assert net.iface("c1", 1).addr == before


def test_a_lease_runs_out_when_no_server_answers():
    net = dhcp_lan()
    r = ScriptRunner(net)
    r.run("dhcpd r1 off\nwait 3599")
    assert net.iface("c1", 1).via == "dhcp"
    r.run("wait 1")
    assert "lease on (1,10) expired (no server answered), released" in plain(r)
    assert net.iface("c1", 1).via == "adhoc" and "r1:1" not in net.dhcp_leases


def test_an_address_found_in_use_is_declined():
    net = dhcp_lan()
    r = ScriptRunner(net)
    r.run("release c1\nip c2 (1,10) 1\nip c1 dhcp\ndhcp c1")
    text = plain(r)
    assert "DECLINE (1,10): an ARP probe found it in use" in text
    assert net.iface("c1", 1).addr == (1, 12)


def test_a_pool_runs_out():
    kinds = ["r"] + [("s", 64)] * 5 + ["c"] * 245
    cables = [("r1", "s1"), ("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s5")]
    cables += [(f"s{1 + i // 58}", f"c{i + 1}") for i in range(245)]
    net = build(kinds, cables)
    assert len({net.iface(f"c{i}", 1).host for i in range(1, 245)}) == 244
    assert net.iface("c245", 1).via == "adhoc" or net.iface("c245", 1).addr is None


def test_a_client_keeps_its_binding_across_an_unplug():
    net = dhcp_lan(2)
    r = ScriptRunner(net)
    mine = net.iface("c1", 1).host
    r.run("unlink c1")
    net.reconcile()
    assert net.iface("c1", 1).addr is None
    net.add("c")
    r.run("c3 s1")
    net.reconcile()
    assert net.iface("c3", 1).host != mine                     # c1's binding is still held for it
    r.run("c1 s1")
    net.reconcile()
    assert net.iface("c1", 1).host == mine


def test_no_lease_across_a_router_without_a_server_on_the_link():
    net = build(["r", "s", "c", "s", "c"], [("r1", "s1"), ("s1", "c1"), ("s2", "c2")])
    assert net.iface("c1", 1).via == "dhcp" and net.iface("c2", 1).via == "adhoc"


def test_the_clock_and_leases_survive_a_save():
    from bytesim.model import Network as N
    net = dhcp_lan()
    ScriptRunner(net).run("wait 100")
    back = N.from_json(net.to_json())
    assert back.clock == 100 and back.iface("c1", 1).lease == 3600


# ---- MLAG peer state -----------------------------------------------------------------------

def mlag(keepalive=True):
    """fab = s1+s2 (peer link s1-s2, keepalive s1:6-s2:6), dual-homed to g1 and to s3 (c1, c2)."""
    from bytesim.scripting import replay
    cmds = ["add g 4 1", "add s 6 3", "add c 1 2", "group fab s1 s2",
            "g1 s1", "g1 s2", "s3 s1", "s3 s2", "c1 s3", "c2 s3", "s1 s2", "mlag fab", "lag auto"]
    cmds += ["s1:6 s2:6 role=control"] if keepalive else []
    net = replay("\n".join(cmds))
    net.reconcile()
    return net, ScriptRunner(net)


def peer(net):
    return next(i for i, l in enumerate(net.links) if {l.a, l.b} == {"s1", "s2"} and not l.control)


def test_flows_use_both_chassis_while_both_are_up():
    net, _ = mlag()
    used = {net._logical_link("s3", 1, f"c{i}>g1") for i in range(200)}
    assert {net.links[i].other("s3")[0] for i in used} == {"s1", "s2"}


def test_peer_link_down_with_keepalive_up_suspends_the_secondary():
    net, r = mlag()
    net.links[peer(net)].up = False
    notes = net.reconcile()
    assert net.mlag_peer_state("fab")[0] == "peer-down"
    assert {d for d, _ in net.mlag_suspended} == {"s2"}
    assert any("s2 (secondary) suspends its MLAG ports" in n for n in notes)
    path = [d for _, _, f in net.route("c1", None) for d, _ in f]
    assert "s2" not in path and "s1" in path
    r.run("mlags")
    assert "peer link down" in plain(r) and "primary s1 carries the bundles" in plain(r)
    net.links[peer(net)].up = True
    net.reconcile()
    assert not net.mlag_suspended


def test_losing_peer_link_and_keepalive_is_dual_active_and_never_silent():
    net, r = mlag()
    for i, l in enumerate(net.links):
        if {l.a, l.b} == {"s1", "s2"}:
            l.up = False
    notes = net.reconcile()
    assert net.mlag_dual == {"fab"} and not net.mlag_suspended
    assert any("DUAL-ACTIVE" in n for n in notes)
    r.run("unicast c1 g1\nmlags")
    assert "crosses MLAG fab, which is dual-active" in plain(r) and "⚠ DUAL-ACTIVE" in plain(r)


def test_without_a_keepalive_a_peer_link_failure_is_dual_active():
    net, _ = mlag(keepalive=False)
    net.links[peer(net)].up = False
    net.reconcile()
    assert net.mlag_peer_state("fab")[0] == "dual-active"


def test_losing_a_whole_chassis_keeps_service():
    for victim in ("s1", "s2"):
        net, r = mlag()
        r.run(f"fail {victim}")
        net.reconcile()
        assert net.mlag_peer_state("fab")[0] == "single" and not net.mlag_suspended
        r.run("unicast c1 g1")
        assert "delivered to g1" in plain(r)


# ---- traceroute ------------------------------------------------------------------------------

def test_traceroute_lists_each_router_then_the_destination():
    r = chain(3)
    r.run("traceroute c1 c2")
    text = plain(r)
    for k, hop in enumerate(["r1 (1,1)", "r2 (2,2)", "r3 (3,2)", "c2 (4,10)"], 1):
        assert f"  {k}  {hop}" in text
    assert "(reached)" in text and "max 30 hops" in text


def test_a_router_whose_answer_cant_get_back_shows_a_star():
    r = chain(3)
    r.run("route r2 1 drop\ntraceroute c1 c2")
    text = plain(r)
    assert "   2  *  (r2's time exceeded can't get back: r2 drops traffic for network 1" in text
    assert "   1  r1 (1,1)" in text and "   3  *" in text      # r3's answer crosses r2 too


def test_traceroute_through_nat_hears_from_routers_on_both_sides():
    net, r = internet()
    r.run("traceroute c1 c7")
    text = plain(r)
    assert "*" not in text and "(reached)" in text and "  1  g3" in text


def test_traceroute_to_the_internet_ends_at_the_exit():
    net, r = internet()
    r.run("traceroute c1 internet")
    assert "then the internet" in plain(r) and "(reached)" not in plain(r)


def test_a_new_echo_request_never_rides_a_mapping_in():
    net, r = internet()
    r.run("ping c1 c7")
    with pytest.raises(NetError, match="dropped by g3's NAT: unsolicited inbound"):
        r.run("ping c7 c1")


# ---- review follow-ups ---------------------------------------------------------------------

def test_reordering_an_active_passive_cluster_rebuilds_the_tables():
    net = build(["c", ("r", 4), ("r", 4), ("r", 4), "c"], [("c1", "r3"), ("r3", "r1"), ("r1", "c2")])
    r = ScriptRunner(net)
    r.run("r1:4 r2:4 role=control\ncluster ha r1 r2 mode=active-passive")
    net.reconcile()
    assert [leg[1][0] for leg in net.route("c1", ("c2", 1))] == ["r3", "r1", "c2"]   # warms the tables
    net.groups["ha"].reverse()                                                    # r2 is active now
    assert net.standby_routers() == {"r1"}
    with pytest.raises(NetError):
        net.route("c1", ("c2", 1))


def test_a_static_route_into_a_nat_lan_still_needs_a_mapping():
    net = build(["c", "g", "g", "c"], [("c1", "g1"), ("g1", "g2"), ("g2", "c2")])
    r = ScriptRunner(net)
    r.run("group isp g2 c2\ninternet isp")
    net.reconcile()
    wan = net.addr_of(next((("g1", p) for p in net.wan_ports("g1"))))
    lan = net.addr_of(("c1", 1))[0]
    r.run(f"route g2 {lan} via ({wan[0]},{wan[1]})")
    assert net.route("c2", ("c1", 1))                          # the route exists...
    with pytest.raises(NetError, match="dropped by g1's NAT: unsolicited inbound"):
        r.run("unicast c2 c1")                                  # ...but NAT still says no
    r.run("unicast c1 c2\nunicast c2 c1")                       # return traffic on c1's flow
    assert "delivered to c1" in plain(r)
    with pytest.raises(NetError, match="unsolicited inbound"):
        r.run("ping c2 c1")                                     # a new echo request isn't a reply


def test_a_connected_network_beats_a_static_route():
    net = build(["c", "r", "c"], [("c1", "r1"), ("r1", "c2")])
    r = ScriptRunner(net)
    c2net = net.addr_of(("c2", 1))[0]
    r.run(f"route r1 {c2net} drop")
    assert "connected to r1: the connected route wins" in plain(r)
    assert net.routing_table("r1")[c2net].kind == "connected"
    r.run("ping c1 c2")
    assert "  reply from c2" in plain(r)



# ---- second review: effects only where the packet got, free NAT ports, real probes ----------

def natnet():
    """g1 (NAT) with c1-c4 on s1, its WAN to g2, and the public c5 behind g2."""
    net = build([("g", 3), ("g", 3), ("s", 8), "c", "c", "c", "c", "c"],
                [("g1", "s1"), ("g1", "g2"), ("g2", "c5")] + [(f"c{i}", "s1") for i in range(1, 5)])
    net.group_add("public", ["g2", "c5"])
    net.internet = "public"
    net.reconcile()
    return net, ScriptRunner(net, detail=True)


def test_a_packet_lost_before_the_nat_gateway_opens_no_mapping():
    net, r = natnet()
    r.run("unicast c1 g1")                                      # c1 caches g1's MAC
    net.learn_mac("s1", net.devices["g1"].mac(1), 3)             # s1 now sends it to c2's port
    with pytest.raises(NetError, match="reached c2 instead"):
        r.run("unicast c1 c5")
    assert not net.nat_table
    with pytest.raises(NetError, match="unsolicited inbound"):
        r.run("unicast c5 c1")


def test_a_packet_lost_before_a_router_sets_off_nothing_beyond_it():
    net = build([("s", 5)] * 4 + [("r", 3), "c", "c", "c"],
                [("c1", "s1"), ("r1", "s1"), ("c3", "s1"), ("r1", "s2"),
                 ("s2", "s3"), ("s3", "s4"), ("s4", "s2"), ("c2", "s3")])
    r = ScriptRunner(net)
    r.run("unicast c1 r1:1")
    net.stp_off = {"s2", "s3", "s4"}
    net.reconcile()
    net.storming.clear()
    net.arp_store(("c1", 1), net.addr_of(("r1", 1)), ("r1", 1))
    net.learn_mac("s1", "R1A", 3)                                # the first hop can't reach r1
    with pytest.raises(NetError):
        r.run("unicast c1 c2")
    assert not net.storming                                      # r1 never got it, so never ARPed


def test_nat_never_hands_out_a_public_port_a_live_flow_holds():
    net, r = natnet()
    for i in (1, 2, 3):
        r.run(f"unicast c{i} c5")
        if i < 3:
            r.run("wait 10")
    r.run("wait 45")                                             # only c1's mapping (40000) expires
    r.run("unicast c4 c5")
    live = [m for m in net.nat_table["g1"] if m["expires"] > net.clock]
    assert len({m["public"] for m in live}) == len(live) == 3
    assert {m["public"][1] for m in live} == {40000, 40001, 40002}  # the freed port, not a held one


def test_nat_ports_can_run_out():
    net, r = natnet()
    net.nat_table["g1"] = [{"inside": ((9, i), 1), "public": (net.addr_of(("g1", 2)), p), "remote": "c5",
                            "expires": 1e9} for i, p in enumerate(net.NAT_PORTS)]
    with pytest.raises(NetError, match="g1's NAT is out of public ports"):
        r.run("unicast c1 c5")


def test_traceroute_shows_the_hops_before_a_router_that_drops():
    r = chain(3)
    net4 = r.net.addr_of(("c2", 1))[0]
    r.run(f"route r2 {net4} drop\ntraceroute c1 c2")
    text = plain(r)
    assert "TRACEROUTE" in text and "   1  r1 (1,1)" in text and "   2  r2 (2,2)" in text
    assert f"   3  *  (r2 can't forward it: r2 drops traffic for network {net4}" in text


def test_traceroute_shows_30_hops_of_a_path_longer_than_a_ping_can_go():
    r = chain(65)
    r.run("traceroute c1 c2")
    text = plain(r)
    assert sum(" ms" in line for line in text.splitlines()) == 30 and "stopped at 30 hops" in text
