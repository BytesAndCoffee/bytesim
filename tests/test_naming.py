import pytest

from bytesim.model import NetError, Network


def test_bfs_and_dfs_visit_orders(basic):
    assert basic.traversal("bfs") == ["g1", "r1", "r2", "s1", "s2", "s3", "c1", "c2", "c3",
                                      "c4", "c5", "c6", "c7", "c8", "c9", "c10"]
    # DFS dives r1 → r2 → s3 and its computers before coming back for s1.
    assert basic.traversal("dfs")[:5] == ["g1", "r1", "r2", "s3", "c6"]


def test_unreachable_devices_are_still_covered(basic):
    order = basic.traversal("dfs", "c9")
    assert order[:2] == ["c9", "c10"] and len(order) == len(basic.devices)


def test_renumber_keeps_every_reference_consistent(basic):
    addr = basic.iface("c6", 1).addr
    groups_before = {g: len(ms) for g, ms in basic.groups.items()}
    mapping = basic.renumber("dfs")
    assert mapping["s3"] == "s1" and mapping["c6"] == "c1"
    assert basic.iface("c1", 1).addr == addr                 # the address followed its device
    assert all(l.a in basic.devices and l.b in basic.devices for l in basic.links)
    assert {g: len(ms) for g, ms in basic.groups.items()} == groups_before
    assert all(k.split(":")[0] in basic.devices for k in basic.ifaces)
    assert basic.counters["c"] == 10 and basic.add("c").id == "c11"


def test_renumber_compacts_gaps_and_is_idempotent():
    net = Network()
    for _ in range(3):
        net.add("r")
    net.remove("r1")
    net.connect("r2", None, "r3", None)
    assert net.renumber("bfs") == {"r2": "r1", "r3": "r2"}
    assert net.renumber("bfs") == {}


def test_bad_order_or_root():
    net = Network()
    net.add("r")
    with pytest.raises(NetError):
        net.traversal("zigzag")
    with pytest.raises(NetError):
        net.traversal("bfs", "r9")
