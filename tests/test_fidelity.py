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
