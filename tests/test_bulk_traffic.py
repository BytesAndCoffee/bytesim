"""Routing on large fabrics: scoped indexes and query caches never outlive a change."""
import pytest

from bytesim import NetError, Network


def network(count=12):
    net = Network()
    net.add("s", count)
    for _ in range(count):
        dev = net.add("c")
        net.connect("s1", None, dev.id, None)
    net.reconcile()
    net.auto_layout(120)
    return net


def test_routing_indexes_and_query_results_do_not_survive_changes():
    net = network(3)
    assert net.route("c1", ("c2", 1))
    link = net.link_on("c2", 1)
    with net.remembering():
        assert ("c2", 1) in net.segment("c1", 1)
        link.up = False
        assert ("c2", 1) not in net.segment("c1", 1)
        assert net.l2path(("c1", 1), ("c2", 1)) is None
    with pytest.raises(NetError):
        net.route("c1", ("c2", 1))
    assert net._memo is None and net._route_memo is None
    link.up = True
    assert net.route("c1", ("c2", 1))
    net.disconnect("c1", None, None)
    net.resize("c3", 2)
    net.connect("s1", 1, "c3", 2)  # reuses the first cable's old switch port
    with net.remembering():
        assert net.link_index("c1", 1) is None
        assert net.link_on("s1", 1).b == "c3"
        assert net.adjacent_links("c1") == []
    assert net._memo is None
