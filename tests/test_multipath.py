"""ECMP across equal-cost routes, and link aggregation across parallel cables."""
from collections import Counter

import pytest

from bytesim import demos
from bytesim.model import NetError, Network
from bytesim.model.equivalence import differences
from bytesim.scripting import ScriptRunner, replay
from bytesim.synthesis import MODES, synthesize
from bytesim.topology import dump_topology, load_topology


def routers_on(net, a, b):
    return [x[0] for x, _, _ in net.route(a, (b, 1)) if net.is_router(x[0])]


@pytest.fixture
def active_active_core():
    net, _ = demos.core()
    net.cluster_modes.clear()                  # both core routers forward
    net.reconcile()
    return net


# ---- ECMP -----------------------------------------------------------------------------------
def test_ecmp_spreads_flows_over_equal_paths(active_active_core):
    used = Counter()
    for a in ("c1", "c2"):
        for b in ("c3", "c4", "c5", "c6"):
            used.update(r for r in routers_on(active_active_core, a, b) if r in ("r1", "r2"))
    assert used["r1"] and used["r2"]                       # both core routers carry flows


def test_a_flow_always_takes_the_same_path(active_active_core):
    first = active_active_core.route("c1", ("c5", 1))
    assert all(active_active_core.route("c1", ("c5", 1)) == first for _ in range(5))
    again = Network.from_json(active_active_core.to_json())
    again.reconcile()
    assert [x for x, _, _ in again.route("c1", ("c5", 1))] == [x for x, _, _ in first]


def test_ecmp_never_lengthens_a_path(active_active_core):
    # every chosen path is as short as the shortest one: 3 cables to the far site's LAN
    for b in ("c3", "c5"):
        assert sum(len(f) - 1 for _, _, f in active_active_core.route("c1", (b, 1))) == 6


def test_unique_paths_are_unchanged_and_standbys_still_rest():
    net, _ = demos.core()
    net.reconcile()
    assert all(routers_on(net, "c1", b).count("r1") == 1 and "r2" not in routers_on(net, "c1", b)
               for b in ("c3", "c4", "c5", "c6"))


def test_traffic_loads_both_routers_of_an_active_active_core():
    r = ScriptRunner()
    r.run("demo core\ncluster core mode=active-active\ntraffic on 10 seed=4 mix=unicast")
    r.net.reconcile()
    for _ in range(40):
        r.traffic_step(0.5)
    st = r.traffic.stats
    assert st.forwarded["r1"] > 5 and st.forwarded["r2"] > 5


# ---- link aggregation -------------------------------------------------------------------------
def pair_of_switches(lag=True, stp=True):
    cmds = ["add s 6 2", "add c 1 6", "s1 s2", "s1 s2", "c1 s1", "c2 s1", "c3 s1", "c4 s2", "c5 s2", "c6 s2"]
    cmds += ["lag s1 s2"] if lag else []
    cmds += [] if stp else ["stp s1 s2 off"]
    net = replay("\n".join(cmds))
    net.reconcile()
    return net


def test_a_lag_is_not_a_loop():
    assert pair_of_switches(lag=False).stp_blocked == {("s2", 2)}
    net = pair_of_switches()
    assert net.stp_blocked == set() and net.loop_links(honor_stp=False) == set()


def test_flows_spread_across_members_and_survive_a_failure():
    net = pair_of_switches()
    used = Counter()
    for a in ("c1", "c2", "c3"):
        for b in ("c4", "c5", "c6"):
            path = net.l2path((a, 1), (b, 1), f"{a}>{b}")
            used[next(li for d, li in path if d == "s2")] += 1
    assert set(used) == {0, 1}
    net.links[0].up = False
    assert all(next(li for d, li in net.l2path((a, 1), ("c4", 1), f"{a}>c4") if d == "s2") == 1
               for a in ("c1", "c2", "c3"))


def test_a_broadcast_crosses_a_lag_once_instead_of_storming():
    assert pair_of_switches(lag=False, stp=False).flood(("c1", 1)).storm
    fl = pair_of_switches(stp=False).flood(("c1", 1))
    assert not fl.storm and set(fl.delivered.values()) == {1}


def test_lag_rules():
    with pytest.raises(NetError, match="at least two"):
        replay("add s 4 2\ns1 s2\nlag s1 s2")
    with pytest.raises(NetError, match="routed links"):
        replay("add r 4 2\nr1 r2\nr1 r2\nlag r1 r2")             # router↔router: ECMP's job
    with pytest.raises(NetError, match="needs a switch"):
        replay("add s 4 1\nadd c 2 1\nc1 s1\nc1 s1\nlag c1 s1")
    net = replay("add s 4 2\ns1 s2\ns1 s2\nlag s1 s2\nunlag s1 s2")
    assert net.lags == set()


@pytest.mark.parametrize("mode", MODES)
def test_lags_round_trip_everywhere(mode):
    net = pair_of_switches()
    net.group_add("pod", ["s1", "s2"])
    assert Network.from_json(net.to_json()).lags == net.lags
    for pin in (True, False):
        assert load_topology(dump_topology(net, pin_ports=pin)).lags == net.lags
    assert differences(net, replay(synthesize(net, mode).script)) == []


def test_clone_copies_a_lag_and_synthesis_reuses_it():
    net = replay("add s 4 2\ns1 s2\ns1 s2\nlag s1 s2\ngroup pod1 s1 s2\nclone pod1 pod2")
    assert frozenset({"s3", "s4"}) in net.lags
    res = synthesize(net, "shortest")
    assert any(s.kind == "clone" for s in res.used)
    assert differences(net, replay(res.script)) == []


def test_deleting_or_renumbering_keeps_lags_consistent():
    net = replay("add s 4 3\ns2 s3\ns2 s3\nlag s2 s3\ndel s1")
    net.renumber("bfs")
    assert net.lags == {frozenset({"s1", "s2"})}
    net.remove("s1")
    assert net.lags == set()



# ---- switch ↔ gateway / router spans -------------------------------------------------------------
def gateway_span(lag=True):
    """A gateway with two cables to a switch, three computers behind the switch."""
    cmds = ["add g 4 1", "add s 6 1", "add c 1 3", "g1 s1", "g1 s1", "c1 s1", "c2 s1", "c3 s1"]
    net = replay("\n".join(cmds + (["lag s1 g1"] if lag else [])))
    net.reconcile()
    return net


def test_aggregatable_spans_are_detected():
    net = replay("add g 4 1\nadd r 4 2\nadd s 6 2\nadd c 2 1\n"
                 "g1 s1\ng1 s1\ns1 s2\ns1 s2\nr1 s2\nr1 s2\nr1 r2\nr1 r2\nc1 s2\nc1 s2\ns1 r2")
    assert net.aggregatable() == [("s1", "g1", 2), ("s1", "s2", 2), ("s2", "r1", 2)]   # not r1–r2, not c1
    net.lags.add(frozenset({"s1", "g1"}))
    assert ("s1", "g1", 2) not in net.aggregatable()


def test_a_bundled_gateway_port_is_one_interface():
    before, after = gateway_span(lag=False), gateway_span()
    assert len(before.addressed("g1")) == 2                         # two ports, two addresses
    assert [p for p, _ in after.addressed("g1")] == [1]             # one interface on the primary


def test_traffic_to_the_gateway_spreads_and_survives_a_member_failure():
    net = gateway_span()
    used = {next(li for d, li in net.route(c, None)[0][2] if d == "g1") for c in ("c1", "c2", "c3")}
    assert used == {0, 1}                                           # both member cables carry flows
    net.links[0].up = False                                         # the primary port's own cable fails
    net.reconcile()
    assert [p for p, _ in net.addressed("g1")] == [1]               # the interface keeps its address
    for c in ("c1", "c2", "c3"):
        assert net.route(c, None)[0][2][-1][1] == 1                 # …and traffic uses the other member


def test_a_broadcast_reaches_a_bundled_gateway_once():
    assert gateway_span(lag=False).flood(("c1", 1)).delivered[("g1", 1)] + \
        gateway_span(lag=False).flood(("c1", 1)).delivered[("g1", 2)] == 2
    fl = gateway_span().flood(("c1", 1))
    assert sum(n for (d, _), n in fl.delivered.items() if d == "g1") == 1


@pytest.mark.parametrize("mode", MODES)
def test_switch_gateway_lags_round_trip(mode):
    net = gateway_span()
    assert Network.from_json(net.to_json()).lags == net.lags
    assert load_topology(dump_topology(net, pin_ports=True)).lags == net.lags
    assert differences(net, replay(synthesize(net, mode).script)) == []
