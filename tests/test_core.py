"""A redundant routed core: inter-site traffic must cross it; local traffic must not."""
import pytest

from bytesim import demos
from bytesim.model import NetError, Network
from bytesim.model.equivalence import differences
from bytesim.scripting import ScriptRunner, replay
from bytesim.synthesis import MODES, synthesize
from bytesim.topology import dump_topology, load_topology

SITES = {1: ("c1", "c2"), 2: ("c3", "c4"), 3: ("c5", "c6")}
CORE = {"r1", "r2"}


@pytest.fixture
def core() -> Network:
    net, _ = demos.core()
    net.reconcile()
    return net


def hops(net, a, b):
    """Every device a packet from a to b passes through (raises NetError if it can't)."""
    net.reconcile()
    path = [a]
    for _, _, frames in net.route(a, (b, 1)):
        path += [d for d, _ in frames[1:]]
    return path


@pytest.mark.parametrize("x, y", [(1, 2), (1, 3), (2, 3)])
def test_traffic_between_sites_crosses_the_active_core(core, x, y):
    for a in SITES[x]:
        for b in SITES[y]:
            assert hops(core, a, b).count("r1") == 1 and "r2" not in hops(core, a, b)


def test_local_traffic_never_needs_the_core(core):
    for a, b in SITES.values():
        assert not CORE & set(hops(core, a, b))
    core.devices["r1"].up = core.devices["r2"].up = False
    assert hops(core, "c1", "c2") == ["c1", "s1", "c2"]


def test_no_two_sites_share_a_network(core):
    site_routers = {1: {"r3", "r4"}, 2: {"r5", "r6"}, 3: {"r7", "r8"}}
    for dev, routers in site_routers.items():
        for r in routers:
            for p in range(1, core.devices[r].nports + 1):
                seg = {d for d, _ in core.segment(r, p)}
                others = set().union(*(v for k, v in site_routers.items() if k != dev))
                assert not seg & others, f"{r}:p{p} shares a network with another site"


def test_the_standby_takes_over_when_the_active_fails(core):
    core.devices["r1"].up = False
    assert core.ha_state("core")["forwarders"] == {"r2"}
    for x, y in [(1, 2), (1, 3), (2, 3)]:
        assert "r2" in hops(core, SITES[x][0], SITES[y][0])


def test_losing_both_core_routers_isolates_the_sites_only(core):
    core.devices["r1"].up = core.devices["r2"].up = False
    with pytest.raises(NetError):
        hops(core, "c1", "c3")
    assert hops(core, "c3", "c4") == ["c3", "s2", "c4"]


def test_a_failed_uplink_uses_the_other_router_of_the_site(core):
    li = next(i for i, l in enumerate(core.links) if {l.a, l.b} == {"r3", "r1"})
    core.links[li].up = False
    path = hops(core, "c1", "c3")
    assert "r1" in path and path[path.index("r1") - 1] == "r4"     # r3 → r4 → r1 instead


def test_one_control_link_down_is_degraded_but_still_one_cluster(core):
    core.links[core.link_index("r1", 7)].up = False
    st = core.ha_state("core")
    assert st["level"] == "degraded" and len(st["partitions"]) == 1 and "r1" in hops(core, "c1", "c3")


@pytest.mark.parametrize("policy, forwarding", [("witness", {"r1"}), ("quorum", set()), ("none", {"r1", "r2"})])
def test_both_control_links_down_follow_the_split_policy(core, policy, forwarding):
    core.clusters["core"] = policy
    for p in (7, 8):
        core.links[core.link_index("r1", p)].up = False
    assert core.ha_state("core")["forwarders"] == forwarding
    if not forwarding:                                              # quorum: no side may lead
        with pytest.raises(NetError):
            hops(core, "c1", "c3")
    else:
        assert forwarding & set(hops(core, "c1", "c3"))


def test_active_active_lets_both_forward(core):
    core.cluster_modes.pop("core")
    assert core.ha_state("core")["forwarders"] == CORE and core.standby_routers() == set()


def test_background_traffic_loads_the_core():
    r = ScriptRunner()
    r.run("demo core\ntraffic on 10 seed=3 mix=unicast")
    r.net.reconcile()
    for _ in range(30):
        r.traffic_step(0.5)
    st = r.traffic.stats
    assert st.forwarded["r1"] > 0 and st.forwarded["r2"] == 0       # the standby carries nothing
    assert any("r1:" in cable for cable, _ in st.cables.most_common(5))


@pytest.mark.parametrize("mode", MODES)
def test_round_trips_json_yaml_and_scripts(core, mode):
    assert differences(core, Network.from_json(core.to_json())) == []
    for pin in (True, False):
        back = load_topology(dump_topology(core, pin_ports=pin))
        assert back.cluster_modes == {"core": "active-passive"} and back.clusters == core.clusters
    assert differences(core, replay(synthesize(core, mode).script)) == []


def test_synthesis_reuses_a_site_and_its_core_uplinks():
    # The shortest script copies site 1 for sites 2 and 3, rewiring the uplinks onto the core.
    res = synthesize(demos.core()[0], "shortest")
    assert sum(1 for s in res.used if s.kind in ("copy", "ports") and s.pairs == [("core", "core")]) == 2
    assert res.commands < res.explicit_commands


def test_a_member_that_stood_down_stops_forwarding_in_active_active_too(core):
    core.cluster_modes.pop("core")                                   # active-active
    assert "r1 + r2 active" in core.ha_state("core")["text"]
    for p in (7, 8):                                                 # split; witness keeps r1's side
        core.links[core.link_index("r1", p)].up = False
    assert core.ha_state("core")["forwarders"] == {"r1"} and core.standby_routers() == {"r2"}
    for a in ("c1", "c2"):
        for b in ("c3", "c4", "c5", "c6"):
            assert "r2" not in hops(core, a, b)
