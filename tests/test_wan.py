"""The public internet between sites, and NAT on the gateways that face it."""
import re

import pytest

from bytesim import NetError, Network, demos
from bytesim.model.equivalence import differences
from bytesim.model.traffic import drop_reason
from bytesim.scripting import ScriptRunner, replay
from bytesim.synthesis import MODES, synthesize


def runner():
    net, _ = demos.internet()
    net.reconcile()
    net.auto_layout(120)
    return ScriptRunner(net, detail=True)


def text(r):
    return re.sub(r"\[/?[^\]]*\]", "", "\n".join(r.log))       # without markup


def test_only_site_gateways_have_wan_ports_and_run_nat():
    net = runner().net
    assert {d: net.wan_ports(d) for d in ("g1", "g2", "g3", "g4", "g5")} == {
        "g1": set(), "g2": set(), "g3": {1}, "g4": {1}, "g5": {1}}
    assert all(net.natting(g) for g in ("g3", "g4", "g5"))
    assert not net.natting("g1") and net.public() == {"g1", "g2", "s1"}


def test_outbound_is_translated_and_its_reply_comes_back():
    r = runner()
    r.run("ping c1 c7")
    assert "NAT at g3: (6,10):" in text(r) and "(its WAN address" in text(r)
    assert "reply from c7" in text(r)


@pytest.mark.parametrize("src, dst, gw", [("c1", "c3", "g4"), ("c7", "c1", "g3"), ("c5", "c2", "g3")])
def test_unsolicited_inbound_is_dropped_at_the_destination_nat(src, dst, gw):
    r = runner()
    with pytest.raises(NetError, match=f"dropped by {gw}'s NAT: unsolicited inbound"):
        r.run(f"ping {src} {dst}")


def test_nat_off_opens_a_site_and_on_closes_it_again():
    r = runner()
    r.run("nat g4 off\nping c1 c3")
    assert "reply from c3" in text(r)
    r.run("nat g4 on")
    with pytest.raises(NetError, match="g4's NAT"):
        r.run("ping c1 c3")


def test_multicast_replies_get_back_through_nat_but_requests_into_sites_do_not():
    r = runner()
    r.run("mping c1 all")
    t = text(r)
    assert "reply from c7 to c1" in t and "reply from c2 to c1" in t
    assert "multicast ping: 2/6 replies, 4 failed" in t


def test_the_internet_means_the_declared_internet():
    r = runner()
    r.run("ping c1 internet")
    assert "c1 → s2 → g3 → g1 → ☁" in text(r)


def test_nat_drops_have_their_own_traffic_reason():
    assert drop_reason("dropped by g4's NAT: unsolicited inbound traffic from the internet to …") == "NAT"


def test_commands_validate_and_report():
    r = runner()
    with pytest.raises(NetError, match="not a gateway"):
        r.run("nat s1 off")
    r.run("group pcs c1 c2\ninternet pcs")                 # allowed, with a warning
    assert "pcs has no gateway yet" in text(r)
    r.run("internet isp")
    r.run("internet")
    assert "g4: WAN p1" in text(r) and "easy NAT" in text(r)
    r.run("internet off")
    assert r.net.internet is None and not r.net.wan_ports("g3")
    r.run("ping c1 c3")                                   # no internet: plain routing again
    assert "reply from c3" in text(r)


def test_removing_the_internet_group_forgets_it():
    r = runner()
    r.run("ungroup isp")
    assert r.net.internet is None


@pytest.mark.parametrize("mode", MODES)
def test_internet_and_nat_round_trip(mode):
    net = runner().net
    net.nat_off.add("g5")
    back = Network.from_json(net.to_json())
    assert back.internet == "isp" and back.nat_off == {"g5"}
    assert differences(net, replay(synthesize(net, mode).script)) == []
