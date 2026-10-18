import pytest

from bytesim.model import NetError


def test_routers_take_low_hosts_and_clients_lease_from_10(basic):
    r1 = basic.addressed("r1")
    assert all(ifc.host == 1 or ifc.host == 2 for _, ifc in r1)
    c1 = basic.iface("c1", 1)
    assert (c1.host, c1.via) == (10, "dhcp")
    assert c1.gw == basic.addr_of(basic.dhcp_server(("c1", 1)))[1]


def test_networks_span_switches_and_are_unique(basic):
    # c1 and c2 hang off the same switch: same network. c6 is behind r2: a different one.
    assert basic.iface("c1", 1).net == basic.iface("c2", 1).net != basic.iface("c6", 1).net
    assert len({i.net for i in basic.ifaces.values() if i.addr}) >= 5


def test_adhoc_pair_shares_a_fresh_network(basic):
    a, b = basic.iface("c9", 1), basic.iface("c10", 1)
    assert a.via == b.via == "adhoc" and a.net == b.net and a.host != b.host


def test_routing_follows_gateways(basic):
    legs = basic.route("c1", ("c8", 1))
    assert [a[0] for a, _, _ in legs] == ["c1", "r1", "r2"]
    with pytest.raises(NetError, match="no gateway"):
        basic.route("c9", ("c1", 1))


def test_unplugging_releases_and_replugging_into_a_router_leases(basic):
    basic.disconnect("c4", None, None)
    basic.reconcile()
    assert basic.iface("c4", 1).addr is None
    basic.connect("c4", None, "s2", None)
    basic.reconcile()
    assert basic.iface("c4", 1).via == "dhcp"


def test_a_router_joining_adopts_the_adhoc_network(basic):
    net_id = basic.iface("c9", 1).net
    basic.disconnect("c9", None, None)
    basic.add("s")
    basic.add("r")
    for a in ("c9", "c10"):
        basic.disconnect(a, None, None)
        basic.connect(a, None, "s4", None)
    basic.reconcile()
    adhoc_net = basic.iface("c9", 1).net
    basic.connect("r3", None, "s4", None)
    basic.reconcile()
    assert basic.iface("c9", 1).net == adhoc_net
    assert basic.iface("c9", 1).via == "dhcp"
    assert net_id  # the old pair had a network too
