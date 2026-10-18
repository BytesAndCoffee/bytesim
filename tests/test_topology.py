import pytest

from bytesim.model import NetError, Network


def test_ids_are_per_kind_and_never_reused():
    net = Network()
    assert [net.add("r").id, net.add("r").id, net.add("c").id] == ["r1", "r2", "c1"]
    net.remove("r2")
    assert net.add("r").id == "r3"


def test_connect_picks_first_free_port_and_rejects_bad_ports():
    net = Network()
    net.add("s", 2)
    net.add("c")
    net.add("c")
    net.add("c")
    assert net.connect("s1", None, "c1", None).pa == 1
    assert net.connect("s1", None, "c2", None).pa == 2
    with pytest.raises(NetError, match="no free ports"):
        net.connect("s1", None, "c3", None)
    with pytest.raises(NetError, match="no port 9"):
        net.connect("c3", 9, "s1", None)
    with pytest.raises(NetError, match="itself"):
        net.connect("c3", None, "c3", None)


def test_resize_refuses_to_cut_a_used_port():
    net = Network()
    net.add("r", 4)
    net.add("c")
    net.connect("r1", 4, "c1", None)
    with pytest.raises(NetError):
        net.resize("r1", 2)
    net.resize("r1", 8)
    assert net.devices["r1"].nports == 8


def test_macs_are_device_id_plus_port_letter():
    net = Network()
    r = net.add("r", 28)
    assert [r.mac(1), r.mac(2), r.mac(26), r.mac(27)] == ["R1A", "R1B", "R1Z", "R1AA"]


def test_json_round_trip(basic):
    again = Network.from_json(basic.to_json())
    assert again.to_json() == basic.to_json()
