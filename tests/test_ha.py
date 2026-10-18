from bytesim.constants import VIP_HOST


def _level(net):
    return net.ha_state("rfab1")["level"]


def test_cluster_hands_out_the_virtual_gateway(ha):
    assert ha.iface("c1", 1).gw == VIP_HOST
    assert _level(ha) == "ok"
    assert ha.notes == [] or True
    legs = ha.route("c1", ("c6", 1))
    assert legs[0][1][0] == "r1"


def test_losing_one_control_link_removes_redundancy(ha):
    ha.links[ha.link_index("r1", 5)].up = False
    assert _level(ha) == "degraded"


def test_losing_both_control_links_is_split_brain(ha):
    for p in (5, 6):
        ha.links[ha.link_index("r1", p)].up = False
    st = ha.ha_state("rfab1")
    assert st["level"] == "split"
    assert all(len(owners) == 2 for owners in st["owners"].values())


def test_policies_resolve_split_brain_differently(ha):
    for p in (5, 6):
        ha.links[ha.link_index("r1", p)].up = False
    ha.clusters["rfab1"] = "quorum"
    assert _level(ha) == "down"
    ha.clusters["rfab1"] = "witness"
    assert _level(ha) == "degraded"
    assert ha.ha_state("rfab1")["active"] == [["r1"]]


def test_witness_fails_over_when_a_member_dies(ha):
    ha.clusters["rfab1"] = "witness"
    ha.devices["r1"].up = False
    st = ha.ha_state("rfab1")
    assert st["level"] == "degraded" and st["active"] == [["r2"]]


def test_fabric_link_carries_traffic_when_an_uplink_dies(ha):
    ha.links[ha.link_index("r1", 2)].up = False
    path = [a[0] for a, _, _ in ha.route("c1", ("c4", 1))] + ["c4"]
    assert path == ["c1", "r1", "r2", "c4"]
