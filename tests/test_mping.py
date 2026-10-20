"""Shared echo requests, independent replies, and partial multicast failures."""
from collections import Counter

import pytest

from bytesim import NetError, Network, demos
from bytesim.scripting import ScriptRunner


def runner(demo="basic"):
    net, _ = demos.DEMOS[demo]()
    net.reconcile()
    net.auto_layout(100)
    return ScriptRunner(net)


def log_text(r):
    return "\n".join(r.log)


@pytest.mark.parametrize("command", ["mping c1 c2 c6", "ping c1 c2 c6", "mcast ping c1 c2 c6",
                                      "multicast-ping c1 c2 c6", "mcastping c1 c2 c6"])
def test_multicast_ping_receives_individual_replies(command):
    r = runner()
    r.run(command)
    text = log_text(r)
    assert "MULTICAST PING" in text and "copies made at:" in text
    assert "reply from c2" in text and "reply from c6" in text
    assert "multicast ping: 2/2 replies, 0 failed" in text
    assert r.net.devices["c2"].mac(1) in r.net.mac_tables["s1"]
    assert r.net.devices["c6"].mac(1) in r.net.mac_tables["s3"]


def test_nested_groups_deduplicate_receivers_and_exclude_the_sender():
    r = runner()
    r.run("group pcs c1 c2 c3\ngroup room pcs c4\nping c1 room c2 c4 c1")
    text = log_text(r)
    assert text.count("reply from c2") == 1 and text.count("reply from c3") == 1
    assert text.count("reply from c4") == 1 and "reply from c1" not in text
    assert "multicast ping: 3/3 replies, 0 failed" in text


def test_ping_all_reports_partial_reachability():
    r = runner()
    r.run("ping c1 all")
    text = log_text(r)
    assert "unreachable: c9" in text and "unreachable: c10" in text
    assert "reply from c2" in text and "reply from c8" in text
    assert "multicast ping: 7/9 replies, 2 failed" in text


def test_a_missing_return_route_is_not_a_successful_reply():
    r = runner()
    r.net.iface("c6", 1).gw = None
    r.run("mping c1 c2 c6")
    text = log_text(r)
    assert "reply from c2" in text and "no reply from c6" in text
    assert "c6 has no gateway" in text
    assert "multicast ping: 1/2 replies, 1 failed" in text


def test_no_reachable_receivers_still_reports_a_summary():
    r = runner()
    r.run("mping c9 c1 c2")
    assert "multicast ping: 0/2 replies, 2 failed" in log_text(r)


def test_graph_multicast_ping_works_without_addresses_or_mac_learning():
    r = runner()
    r.run("mode graph\nmping s1 c1 c6")
    text = log_text(r)
    assert "reply from c1" in text and "reply from c6" in text
    assert "multicast ping: 2/2 replies, 0 failed" in text
    assert not r.net.mac_tables


@pytest.mark.parametrize("command", ["mping", "mping c1", "mping c1 c1"])
def test_invalid_multicast_ping_commands(command):
    r = runner()
    with pytest.raises(NetError, match="usage: mping|no receivers"):
        r.run(command)


def test_replies_wait_for_request_arrival_and_release_link_highlights(monkeypatch):
    r = runner()
    packets = []
    hot = Counter()
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append((color, on_done)))
    monkeypatch.setattr(r.canvas, "heat", hot.update)
    monkeypatch.setattr(r.canvas, "cool", hot.subtract)
    r.run("mping c1 c2 c3")
    assert len(packets) == 2 and all(color == "bright_magenta" for color, _ in packets)
    assert "reply from" not in log_text(r)
    _, arrived = packets.pop(0)
    arrived()
    assert any(color == "green" for color, _ in packets)
    assert "multicast ping:" not in log_text(r)
    while packets:
        _, done = packets.pop(0)
        done()
    assert all(value == 0 for value in hot.values())
    assert "multicast ping: 2/2 replies, 0 failed" in log_text(r)


def test_return_routing_is_checked_when_the_receiver_gets_the_request(monkeypatch):
    r = runner()
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append(on_done))
    r.run("mping c1 c6")
    r.net.iface("c6", 1).gw = None
    packets.pop(0)()
    assert not packets
    assert "no reply from c6" in log_text(r)
    assert "multicast ping: 0/1 replies, 1 failed" in log_text(r)


@pytest.mark.parametrize("removed", ("c1", "c6"))
def test_deleted_endpoints_are_reported_as_failed_replies(monkeypatch, removed):
    r = runner()
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append(on_done))
    r.run("mping c1 c6")
    r.net.remove(removed)
    packets.pop(0)()
    assert not packets
    assert "no reply from c6" in log_text(r)
    assert "multicast ping: 0/1 replies, 1 failed" in log_text(r)


def test_plain_multicast_remains_one_way():
    r = runner()
    r.run("mcast c1 c2 c6")
    text = log_text(r)
    assert "delivered to 2 receivers" in text
    assert "reply from" not in text and "multicast ping:" not in text


def small_network():
    net = Network()
    net.add("s", 8)
    for _ in range(3):
        d = net.add("c")
        net.connect("s1", None, d.id, None)
    net.reconcile()
    net.auto_layout(100)
    return ScriptRunner(net)


@pytest.mark.parametrize("command", ("mping all c1", "ping all c1", "mcast ping all c1"))
def test_all_senders_can_ping_one_target_without_self_pings(command):
    r = small_network()
    r.run(command)
    text = log_text(r)
    assert "2 senders" in text
    assert text.count("reply from c1") == 2
    assert "to c2:" in text and "to c3:" in text
    assert "source c2" in text and "source c3" in text and "source c1" not in text


def test_all_to_all_starts_every_request_together_and_reports_each_sender(monkeypatch):
    r = small_network()
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append((color, on_done)))
    r.run("mping all all")
    assert len(packets) == 6 and all(color == "bright_magenta" for color, _ in packets)
    assert "reply from" not in log_text(r)
    while packets:
        _, done = packets.pop(0)
        done()
    text = log_text(r)
    assert text.count("multicast ping: 2/2 replies, 0 failed") == 3
    assert text.count("reply from") == 6
    assert all(f"source c{i}" in text for i in range(1, 4))


def test_all_senders_handle_unreachable_targets_per_sender():
    r = runner()
    r.run("mping all c1")
    text = log_text(r)
    assert "9 senders" in text
    assert text.count("reply from c1") == 7
    assert "0/1 replies, 1 failed[/] · source c9" in text
    assert "0/1 replies, 1 failed[/] · source c10" in text


def test_all_to_all_can_be_queued_and_then_started():
    r = small_network()
    r.run("pause\nmping all all")
    assert len(r.pending_actions) == 1 and "reply from" not in log_text(r)
    r.run("resume")
    assert log_text(r).count("reply from") == 6 and not r.pending_actions


def test_all_senders_reports_empty_networks_and_self_only_targets():
    r = ScriptRunner()
    with pytest.raises(NetError, match="no computers"):
        r.run("mping all all")
    r.net.add("c")
    with pytest.raises(NetError, match="no receivers"):
        r.run("mping all all")


def mixed_network():
    net = Network()
    net.add("g", 4)
    net.add("r", 4)
    net.add("s", 8)
    net.add("c", 2)  # multiple interfaces still count as one participant
    net.add("c")
    for dev in ("g1", "r1", "c1", "c2"):
        net.connect("s1", None, dev, None)
    net.connect("s1", None, "c1", 2)
    net.add("c")  # c3 is unaddressed; can't participate in address-mode ping
    net.reconcile()
    net.auto_layout(100)
    return ScriptRunner(net)


@pytest.mark.parametrize("command", ("mping all all *", "ping all all *", "mcast ping all all *",
                                     "mcastping all all *", "multicast-ping all all *"))
def test_whole_network_includes_routers_and_gateways_once_per_device(command):
    r = mixed_network()
    r.run(command)
    text = log_text(r)
    assert "4 senders · 12 requests · whole network" in text
    assert text.count("multicast ping: 3/3 replies, 0 failed") == 4
    for src in ("g1", "r1", "c1", "c2"):
        assert f"source {src}" in text
        assert text.count(f"reply from {src}[/]") == 3
        assert f"reply from {src}[/] to {src}:" not in text
    assert "source s1" not in text and "source c3" not in text


def test_whole_network_can_expand_just_sources_or_just_targets():
    r = mixed_network()
    r.run("mping all c1 *")
    assert "3 senders · 3 requests · whole network" in log_text(r)
    assert log_text(r).count("reply from c1") == 3
    r.log.clear()
    r.run("mping c1 all *")
    text = log_text(r)
    assert "reply from g1" in text and "reply from r1" in text and "reply from c2" in text
    assert "multicast ping: 3/3 replies, 0 failed" in text


def test_whole_network_in_graph_mode_includes_switches_and_unaddressed_devices():
    r = mixed_network()
    r.run("mode graph\nmping all all *")
    text = log_text(r)
    assert "6 senders · 30 requests · whole network" in text
    assert "source s1" in text and "source c3" in text
    assert text.count("multicast ping: 4/5 replies, 1 failed") == 5
    assert "multicast ping: 0/5 replies, 5 failed[/] · source c3" in text


def test_whole_network_keeps_configured_failed_devices_in_the_results():
    r = mixed_network()
    r.net.devices["r1"].up = False
    r.run("mping all all *")
    text = log_text(r)
    assert "4 senders · 12 requests" in text
    assert text.count("multicast ping: 2/3 replies, 1 failed") == 3
    assert "multicast ping: 0/3 replies, 3 failed[/] · source r1" in text


@pytest.mark.parametrize("command", ("mping all all", "mping * *"))
def test_default_all_and_two_argument_star_still_select_only_computers(command):
    r = mixed_network()
    r.run(command)
    text = log_text(r)
    assert "3 senders · 6 requests" in text
    assert "source g1" not in text and "source r1" not in text
    assert "source c3" in text  # default all retains its old failure reporting


def test_whole_network_without_computers_and_with_no_pingable_devices():
    net = Network()
    net.add("r", 2)
    net.add("g", 2)
    net.connect("r1", None, "g1", None)
    net.reconcile()
    net.auto_layout(100)
    r = ScriptRunner(net)
    r.run("mping all all *")
    assert "2 senders · 2 requests" in log_text(r)
    assert log_text(r).count("multicast ping: 1/1 replies, 0 failed") == 2
    r = ScriptRunner()
    r.net.add("s")
    with pytest.raises(NetError, match="no pingable devices"):
        r.run("mping all all *")


def test_whole_network_mode_can_be_paused_and_queued():
    r = mixed_network()
    r.run("detail on\npause\nmping all all *")
    assert len(r.pending_actions) == 1 and r.pending_actions[0] == ["mping", "all", "all", "*"]
    r.run("resume")
    assert log_text(r).count("multicast ping: 3/3 replies, 0 failed") == 4


def test_multihomed_device_selection_is_deterministic_and_can_be_interface_qualified():
    r = mixed_network()
    r.net.connect("s1", None, "r1", 2)
    r.net.reconcile()
    r.run("mping c2 r1")
    assert "request to r1: c2:nic1" in log_text(r)
    addr = r.net.addr_of(("r1", 1))
    assert f"→ r1:p1 ({addr[0]},{addr[1]})" in log_text(r)  # tie: lowest port
    r.log.clear()
    addr = r.net.addr_of(("r1", 2))
    r.run(f"mping c2 r1:p2 ({addr[0]},{addr[1]})")
    text = log_text(r)
    assert "→ r1:p2 (" in text and "request to r1:2" in text
    assert "multicast ping: 1/1 replies, 0 failed" in text  # address and port deduplicate
    assert "reply from r1:2" in text
    r.log.clear()
    r.run("mping c2 r1:4")
    assert "r1:p4 has no address" in log_text(r)
    assert "multicast ping: 0/1 replies, 1 failed" in log_text(r)


def test_explicit_router_port_does_not_create_out_of_range_interface_config():
    r = mixed_network()
    before = set(r.net.ifaces)
    r.run("mping c1 r1:99")
    assert "r1 has no port 99" in log_text(r)
    assert set(r.net.ifaces) == before


def test_self_is_excluded_even_when_an_interface_is_selected():
    r = mixed_network()
    r.run("mping all c1:2 *")
    text = log_text(r)
    assert "3 senders · 3 requests" in text and "source c1" not in text
    assert text.count("reply from c1:2") == 3
