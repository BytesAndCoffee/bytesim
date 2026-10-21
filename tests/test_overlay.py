"""Overlays: mesh-VPN addresses that reach each other through NAT."""
import json
import re

import pytest

from bytesim import NetError, Network, demos
from bytesim.model.equivalence import canonical, differences
from bytesim.model.types import Iface, fmt
from bytesim.scripting import ScriptRunner, replay
from bytesim.synthesis import MODES, synthesize
from bytesim.topology import dump_topology, load_topology


def runner():
    net, _ = demos.overlay()
    net.reconcile()
    net.auto_layout(120)
    return ScriptRunner(net, detail=True)


def text(r):
    return re.sub(r"\[/?[^\]]*\]", "", "\n".join(r.log))       # without markup


def test_members_get_overlay_addresses_in_join_order():
    net = runner().net
    assert [net.ol_addr("ol", d) for d in ("c1", "c3", "c5", "c7")] == [(100, h) for h in (1, 2, 3, 4)]
    assert net.ol_addr("ol", "c2") is None and net.ol_resolve((100, 3)) == ("ol", "c5")
    assert net.ol_addrs("c1") == [("ol", (100, 1))]


def test_the_tunnel_crosses_nat_that_plain_traffic_cannot():
    r = runner()
    with pytest.raises(NetError, match="g4's NAT"):
        r.run("ping c1 c3")
    r.run("ping c1 c3@ol")
    t = text(r)
    c1, c3 = (fmt(r.net.iface(d, 1).addr) for d in ("c1", "c3"))
    assert "PING (100,1) → (100,2)" in t and f"WireGuard from {c1} to {c3}, direct" in t
    assert "hole punched through g4's NAT" in t and "reply from (100,2) c3" in t


def test_overlay_addresses_work_as_destinations():
    r = runner()
    r.run("ping c1 (100,3)\nunicast c5 c1@ol")
    assert "reply from (100,3) c5" in text(r) and "delivered to (100,1) c1" in text(r)


@pytest.mark.parametrize("cmd, error", [("ping c2 c3@ol", "c2 is not in overlay ol"),
                                        ("ping c1 c4@ol", "c4 is not in overlay ol"),
                                        ("ping c1 c3@nope", "no overlay nope"),
                                        ("fail g4\nping c1 c3@ol", "no direct path .*no relay"),
                                        ("fail c3\nping c1 c3@ol", "c3 is down")])
def test_tunnels_fail_for_real_reasons(cmd, error):
    with pytest.raises(NetError, match=error):
        runner().run(cmd)


def test_join_and_leave_by_device_or_group():
    r = runner()
    r.run("overlay ol pcs1")                                # c1 already a member, c2 joins
    assert r.net.overlay_members("ol") == ["c1", "c3", "c5", "c7", "c2"]
    r.run("overlay ol remove c2\noverlay lab c2 c4")
    assert r.net.overlay_members("ol") == ["c1", "c3", "c5", "c7"]
    assert r.net.overlays["lab"]["net"] == 101
    r.run("ping c2 c4@lab")
    assert "reply from (101,2) c4" in text(r)
    with pytest.raises(NetError, match="is a switch"):
        r.run("overlay ol s2")
    r.run("overlay off lab")
    assert "lab" not in r.net.overlays


def test_deleting_and_renumbering_members_follow_through():
    r = runner()
    r.run("del c3")
    assert r.net.overlay_members("ol") == ["c1", "c5", "c7"]


def test_overlay_networks_are_never_handed_out_to_cabling():
    net = Network()
    for _ in range(120):
        n = net.new_net()
        assert n not in range(100, 110)
        net.ifaces[f"c{n}:1"] = Iface("static", net=n, host=1)


@pytest.mark.parametrize("mode", MODES)
def test_overlays_round_trip(mode):
    net = runner().net
    back = Network.from_json(net.to_json())
    assert back.overlays == net.overlays
    assert differences(net, replay(synthesize(net, mode).script)) == []


# ---- phase 3: NAT types, relays, the coordination server ----------------------------------

def test_two_hard_nats_relay_and_everyone_else_goes_direct():
    net = runner().net
    modes = {(a, b): net.connection(a, b, "ol")[0] for a in ("c1", "c3", "c5", "c7")
             for b in ("c1", "c3", "c5", "c7") if a != b}
    assert {k for k, m in modes.items() if m == "relay"} == {("c3", "c5"), ("c5", "c3")}


def test_relayed_ping_goes_through_the_relay_and_says_why():
    r = runner()
    r.run("ping c3 c5@ol")
    t = text(r)
    assert "→ c8 →" in t and "relayed via c8: both ends are behind hard NAT (g4 and g5)" in t
    assert "reply from (100,3) c5" in t


def test_easy_nat_lets_the_pair_go_direct_again():
    r = runner()
    r.run("nat g5 easy")
    assert r.net.connection("c3", "c5", "ol")[0] == "direct"


def test_without_a_reachable_relay_two_hard_nats_cannot_talk():
    r = runner()
    r.run("fail c8")
    with pytest.raises(NetError, match="no relay is reachable by both"):
        r.run("ping c3 c5@ol")
    r.run("ping c1 c3@ol")                                 # direct pairs don't need it
    r.run("restore all\nrelay off c8")
    with pytest.raises(NetError, match="none set up"):
        r.run("ping c3 c5@ol")


def test_coordination_server_down_blocks_joining_but_not_existing_tunnels():
    r = runner()
    r.run("fail c7\nping c1 c3@ol")
    assert "reply from (100,2) c3" in text(r)
    with pytest.raises(NetError, match="c2 can't join overlay ol: coordination server c7 is down"):
        r.run("overlay ol c2")
    r.run("restore all\noverlay ol c2")
    assert "c2" in r.net.overlay_members("ol")


def test_joining_needs_a_path_to_the_coordination_server():
    r = runner()
    r.run("fail g3:1")                                     # site 1's WAN cable
    with pytest.raises(NetError, match="c2 can't reach coordination server c7"):
        r.run("overlay ol c2")


def test_status_lists_each_peer_as_direct_or_relayed():
    r = runner()
    r.run("overlay ol")
    t = text(r)
    assert "coordination c7" in t and "c5 relay c8" in t and "c3 direct" in t


def test_relays_and_coordination_follow_device_changes():
    r = runner()
    r.run("del c8")
    assert r.net.relays == []
    r.run("del c7")
    assert "coord" not in r.net.overlays["ol"]


# ---- phase 4: subnet routers and exit nodes ----------------------------------------------------

def test_a_subnet_router_carries_members_onto_its_lan():
    r = runner()
    with pytest.raises(NetError, match="g4's NAT"):
        r.run("ping c1 c4")                                # c4 isn't a member, and sits behind NAT
    r.run("overlay ol advertise c3\nping c1 c4")
    t = text(r)
    assert "→ c3 → s3 → c4" in t and "subnet router c3 forwards onto its LAN" in t and "reply from c4" in t


def test_only_members_use_subnet_routes_and_local_traffic_stays_local():
    r = runner()
    r.run("overlay ol advertise c3")
    with pytest.raises(NetError, match="g4's NAT"):
        r.run("ping c2 c4")                                # c2 isn't in the overlay
    r.run("ping c3 c4")
    assert "c3 → s3 → c4" in text(r) and "forwards onto its LAN" not in text(r)


def test_a_relayed_member_reaches_the_subnet_through_a_relay():
    r = runner()
    r.run("overlay ol advertise c3\nping c5 c4")
    assert "relayed via c8" in text(r) and "reply from c4" in text(r)


def test_an_exit_node_carries_internet_traffic():
    r = runner()
    r.run("overlay ol exit=c7\nping c1 internet")
    t = text(r)
    assert "→ c7 → g2 → ☁" in t and "exit node c7: traffic leaves through it" in t
    r.run("ping c7 internet")                               # the exit node itself goes straight out
    assert "c7 → g2 → ☁" in text(r)


def test_routing_members_must_be_members_and_up():
    r = runner()
    with pytest.raises(NetError, match="c2 is not in overlay ol"):
        r.run("overlay ol advertise c2")
    r.run("overlay ol exit=c7\nfail c7")
    with pytest.raises(NetError, match="exit node c7 is down"):
        r.run("ping c1 internet")


# ---- phase 5: access rules ---------------------------------------------------------------------

def test_no_rules_allow_everything_and_the_first_rule_denies_the_rest():
    net = runner().net
    assert net.acl_allows("ol", "c5", "c1")
    r = runner()
    r.run("acl ol allow c1 c3\nping c1 c3@ol")
    assert "reply from (100,2) c3" in text(r)                # replies are always let back
    for cmd in ("ping c3 c1@ol", "ping c1 c5@ol"):
        with pytest.raises(NetError, match="access rules don't let"):
            r.run(cmd)


def test_rules_match_groups_and_star_but_the_internet_must_be_named():
    r = runner()
    r.run("overlay ol exit=c7 \nacl ol allow * *")
    with pytest.raises(NetError, match="don't let c1 reach internet through exit node c7"):
        r.run("ping c1 internet")
    r.run("acl ol allow site1 internet\nping c1 internet")
    assert "exit node c7" in text(r)


def test_rules_cover_subnet_destinations():
    r = runner()
    r.run("overlay ol advertise c3\nacl ol allow c1 c3")
    with pytest.raises(NetError, match="don't let c1 reach c4 through subnet router c3"):
        r.run("ping c1 c4")
    r.run("acl ol allow c1 lan2\nping c1 c4")
    assert "reply from c4" in text(r)


def test_acl_commands_list_remove_and_clear():
    r = runner()
    r.run("acl ol allow c1 c3\nacl ol allow c3 c1\nacl ol remove 1")
    assert r.net.overlays["ol"]["acl"] == [["c3", "c1"]]
    r.run("acl ol clear")
    assert "acl" not in r.net.overlays["ol"]
    with pytest.raises(NetError, match="no overlay"):
        r.run("acl nope")


def test_status_marks_denied_peers():
    r = runner()
    r.run("acl ol allow c1 c3\noverlay ol")
    assert "c3 direct, c5 denied" in text(r)


def test_routes_exit_and_rules_follow_device_changes_and_round_trip():
    r = runner()
    r.run("overlay ol advertise c3\noverlay ol exit=c7\nacl ol allow c1 c3\nacl ol allow site1 internet")
    net = r.net
    assert differences(net, replay(synthesize(net, "readable").script)) == []
    assert Network.from_json(net.to_json()).overlays == net.overlays
    r.run("del c3")
    assert "routes" not in net.overlays["ol"] or net.overlays["ol"]["routes"] == []
    assert net.overlays["ol"]["acl"] == [["site1", "internet"]]


# ---- regressions found by fuzzing --------------------------------------------------------------

def test_pinging_your_own_overlay_address_is_loopback():
    r = runner()
    r.run("ping c1 (100,1)\nping c1 c1@ol")
    assert text(r).count("reply from self (loopback)") == 2
    with pytest.raises(NetError, match="own overlay address"):
        r.run("unicast c1 c1@ol")


def test_a_subnet_router_answers_on_its_own_lan_address():
    r = runner()
    r.run("overlay ol advertise c3\nping c1 c3")
    assert "subnet router c3 answers on its own address" in text(r) and "reply from c3" in text(r)


@pytest.mark.parametrize("cmd", ["acl ol remove 1", "del c1"])
def test_emptying_the_rules_keeps_denying(cmd):
    r = runner()
    r.run(f"acl ol allow c1 c3\n{cmd}")
    assert r.net.overlays["ol"]["acl"] == []
    with pytest.raises(NetError, match="access rules"):
        r.run("ping c5 c3@ol")
    r.run("acl ol clear\nping c5 c3@ol")


def test_rules_naming_a_removed_group_go_with_it():
    r = runner()
    r.run("acl ol allow pcs1 c3\nacl ol allow c5 c3\nungroup pcs1")
    assert r.net.overlays["ol"]["acl"] == [["c5", "c3"]]


def test_leaving_the_overlay_drops_its_routing_roles():
    r = runner()
    r.run("overlay ol advertise c3\noverlay ol exit=c3\noverlay ol remove c3")
    t = r.net.overlays["ol"]
    assert "exit" not in t and "routes" not in t


@pytest.mark.parametrize("setup", ["overlay empty", "overlay off ol\noverlay lab c1 c3 net=104",
                                   "acl ol allow c1 c3",
                                   "acl ol none", "group void c2\ninternet void\nungroup void\ninternet isp"])
def test_odd_states_still_export_scripts_that_rebuild_them(setup):
    r = runner()
    for line in setup.split("\n"):
        try:
            r.run(line)
        except NetError:
            pass
    r.net.overlays.get("ol", {}).get("acl", []).append(["c1", "c3"])     # a duplicate rule in the data
    for mode in MODES:
        assert differences(r.net, replay(synthesize(r.net, mode).script)) == []


def test_command_words_cannot_be_overlay_names():
    r = runner()
    for name in ("remove", "off", "all", "internet"):
        with pytest.raises(NetError):
            r.run(f"overlay {name} c2")


def test_big_overlays_list_members_and_peers_on_request():
    r = runner()
    r.run("overlay ol")
    assert "c5 relay c8" in text(r) and "hard NAT g5" in text(r) and "· public" in text(r)
    r.run("overlay ol peers c3")
    assert "c1 direct, c5 relay c8, c7 direct" in text(r)


def test_saves_are_canonical():
    r = runner()
    r.run("overlay ol exit=c7\noverlay ol advertise c3\nacl ol allow c1 c3")
    j = r.net.to_json()
    assert Network.from_json(j).to_json() == j


# ---- review fixes ------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["overlay new c1 c99 net=104", "overlay ol coord=c8 exit=c2",
                                 "overlay ol c2 exit=c99", "overlay ol lan=maybe coord=c8", "relay c2 s1",
                                 "overlay ol net=105", "overlay ol c2 c4 host=9", "overlay ol bogus=1"])
def test_a_failing_command_changes_nothing(cmd):
    r = runner()
    before = r.net.to_json()
    with pytest.raises(NetError):
        r.run(cmd)
    assert r.net.to_json() == before


def test_overlay_addresses_stay_put_when_others_leave_and_new_members_fill_gaps():
    r = runner()
    r.run("overlay ol remove c3")
    assert r.net.ol_addr("ol", "c7") == (100, 4) and r.net.ol_resolve((100, 2)) is None
    r.run("overlay ol c2")
    assert r.net.ol_addr("ol", "c2") == (100, 2)                    # the lowest free number
    r.run("overlay ol c4 host=9")
    assert r.net.ol_addr("ol", "c4") == (100, 9)
    for mode in MODES:                                              # gaps and pinned numbers rebuild exactly
        assert differences(r.net, replay(synthesize(r.net, mode).script)) == []
    back = Network.from_json(r.net.to_json())
    assert back.ol_addr("ol", "c4") == (100, 9) and back.ol_addr("ol", "c7") == (100, 4)


def test_older_saves_without_host_numbers_number_members_in_order():
    net = runner().net
    raw = json.loads(net.to_json())
    del raw["overlays"]["ol"]["hosts"]
    back = Network.from_json(json.dumps(raw))
    assert [back.ol_addr("ol", d) for d in ("c1", "c3", "c5", "c7")] == [(100, h) for h in (1, 2, 3, 4)]


def test_hand_edited_overlays_load_safely():
    net = runner().net
    raw = json.loads(net.to_json())
    raw["overlays"] = {"a": {"net": 100}, "b": {"net": 100, "members": ["c1", "s1", "zz"], "hosts": {"c1": 999}},
                       "c9": {"net": 101, "members": ["c1"]}, "d": "nonsense"}
    back = Network.from_json(json.dumps(raw))
    assert sorted(back.overlays) == ["a", "b"]
    assert back.overlays["b"]["net"] != 100 and back.overlay_members("b") == ["c1"]
    assert back.ol_addr("b", "c1")[1] == 1


def test_mping_replies_to_overlay_carried_requests_come_back_the_same_way():
    r = runner()
    r.run("overlay ol advertise c3\nmping c1 c4")
    t = text(r)
    assert "ECHO REPLY" in t and "c4 → s3 → c3 → s3 → g4" in t and "reply from c4 to c1" in t


def test_a_group_named_internet_does_not_hijack_the_keyword():
    r = runner()
    r.run("overlay ol exit=c7\ngroup internet c2\nacl ol allow c1 internet")
    assert r.net.acl_allows("ol", "c1", "internet") and not r.net.acl_allows("ol", "c1", "c2")


def _second_subnet(r):
    for c in ("add r 3", "add s 3", "add c 1", "r1 s3", "r1 s5", "c9 s5"):
        r.run(c)
    r.net.reconcile()
    r.run("overlay ol c9\noverlay ol exit=c7")


def test_exit_node_takes_own_site_traffic_unless_lan_is_on():
    r = runner()
    _second_subnet(r)
    with pytest.raises(NetError, match="c4 is in c9's own site.*lan=on keeps it local"):
        r.run("ping c9 c4")
    r.run("overlay ol lan=on\nping c9 c4\nping c9 internet")
    t = text(r)
    assert "c9 → s5 → r1 → s3 → c4" in t and "exit node c7: traffic leaves through it" in t


def test_relay_name_comes_from_the_chosen_relay():
    net = runner().net
    c = net.connection("c3", "c5", "ol")
    assert c.mode == "relay" and c.relay == "c8"
    assert net.peer_table("ol")["c3", "c5"] == "relay c8"


def test_overlay_odds_and_ends():
    r = runner()
    with pytest.raises(NetError, match="word the overlay command uses"):
        r.run("overlay peers c2")
    with pytest.raises(NetError, match="keeps its network"):
        r.run("overlay ol net=105")
    r.run("overlay lab net=104")
    assert r.net.overlays["lab"]["net"] == 104 and "overlay lab: network 104" in text(r)


def test_double_nat_translates_twice():
    # c1 sits behind g3, which sits behind g2, which faces the ISP (g1)
    net = replay("add g 4 3\nadd s 4 1\nadd c 1 2\ng1 s1\ns1 c2\ns1 g2\ng2 g3\ng3 c1\n"
                 "group isp g1 s1 c2\ninternet isp")
    net.reconcile()
    assert net.wan_ports("g2") == {1} and net.wan_ports("g3") == {1}
    notes = net.nat_notes(net.route("c1", ("c2", 1)))
    assert len(notes) == 2
    first_public = notes[0].split("leaves as ")[1].split()[0]
    assert notes[1].startswith(f"NAT at g2: {first_public} leaves as")
    with pytest.raises(NetError, match="g2's NAT"):           # and inbound stops at the outer NAT
        ScriptRunner(net).run("ping c2 c1")


def test_a_second_gateway_does_not_make_a_lan_port_wan():
    # one site, two gateways (g2, g3) to the ISP; their LAN is s2
    net = replay("add g 4 3\nadd s 4 2\nadd c 1 2\ng1 s1\ns1 c2\ng2 s1\ng3 s1\ng2 s2\ng3 s2\ns2 c1\n"
                 "group isp g1 s1 c2\ninternet isp")
    net.reconcile()
    assert net.wan_ports("g2") == {1} and net.wan_ports("g3") == {1}
    with pytest.raises(NetError, match="NAT"):
        ScriptRunner(net).run("ping c2 c1")


def test_a_failed_uplink_does_not_turn_the_lan_into_wan():
    net, _ = demos.internet()
    net.reconcile()
    before = net.wan_ports("g3")
    r = ScriptRunner(net)
    r.run("fail g3:1")
    assert net.wan_ports("g3") == before


# ---- overlay targets in multicast ping and graph mode -------------------------------------------

def test_mping_takes_overlay_targets_and_their_replies_use_the_tunnel():
    r = runner()
    r.run("ping c1 c3@ol c5@ol")
    t = text(r)
    assert "reply from c3@ol to c1" in t and "reply from c5@ol to c1" in t and "2/2 replies" in t
    r.run("mping c3 all@ol")
    assert "reply from c5@ol to c3: hops=10" in text(r)              # relayed both ways via c8


def test_mping_all_over_a_overlay_sends_from_its_members_only():
    r = runner()
    r.run("mping all all@ol")
    t = text(r)
    assert "not in overlay" not in t and t.count("multicast ping: 3/3 replies") == 4


def test_mping_overlay_addresses_and_rules():
    r = runner()
    r.run("mping c1 (100,2) c2")
    assert "reply from c3@ol to c1" in text(r) and "reply from c2 to c1" in text(r)
    r = runner()
    r.run("acl ol allow c1 c3\nmping c1 all@ol")
    assert "1/3 replies, 2 failed" in text(r)


def test_graph_mode_reaches_members_by_cable_and_still_checks_membership():
    r = runner()
    r.run("mode graph\nping c1 c3@ol")
    assert "c1 → s2 → g3 → g1 → g4 → s3 → c3" in text(r)
    with pytest.raises(NetError, match="c2 is not in overlay ol"):
        r.run("ping c2 c3@ol")


# ---- YAML topologies ---------------------------------------------------------------------------

def _yaml_round_trip(net):
    back = load_topology(dump_topology(net, pin_ports=True))
    canon_a, canon_b = canonical(net), canonical(back)
    for key in ("internet", "nat_off", "nat_hard", "relays", "overlays"):
        assert canon_a[key] == canon_b[key], key
    return back


@pytest.mark.parametrize("demo", ["internet", "overlay"])
def test_demos_round_trip_through_yaml(demo):
    net, _ = demos.DEMOS[demo]()
    net.reconcile()
    _yaml_round_trip(net)


def test_every_overlay_setting_round_trips_through_yaml():
    r = runner()
    r.run("overlay ol remove c3\noverlay ol c2\noverlay ol c4 host=9\noverlay ol advertise c5\n"
          "overlay ol exit=c7 lan=on\nacl ol allow site1 internet\nacl ol allow * c7\n"
          "overlay lab c6 net=107\nacl lab none\nnat g3 off\nnat g3 hard")
    text_out = dump_topology(r.net)
    assert "nat: [off, hard]" in text_out and "hosts: {" in text_out and "acl: []" in text_out
    back = _yaml_round_trip(r.net)
    assert back.ol_addr("ol", "c4") == (100, 9) and back.overlays["lab"]["acl"] == []


@pytest.mark.parametrize("bad, error", [
    ("internet: nowhere", "isn't a group"),
    ("overlays: {ol: {members: [c1], exit: c2}}", "isn't a member"),
    ("overlays: {ol: {members: [c1], bogus: 1}}", "unknown key"),
    ("overlays: {ol: {members: [c1], acl: [[c1]]}}", "a pair"),
    ("overlays: {c9: {members: [c1]}}", "overlay name"),
    ("devices: {s1: {ports: 2, nat: hard}}", "only gateways"),
    ("devices: {g1: {ports: 2, nat: sideways}}", "use on, off, easy or hard"),
])
def test_yaml_overlay_errors_say_where(bad, error):
    base = "schema: bytesim/topology-v1\ndevices: {g1: 2, c1: 1, c2: 1}\ngroups: {isp: [g1]}\n"
    text_in = base + bad + "\n" if not bad.startswith("devices") else bad.replace("devices: {", "devices: {c1: 1, ") + "\n"
    with pytest.raises(NetError, match=error):
        load_topology("schema: bytesim/topology-v1\n" + text_in if bad.startswith("devices") else text_in)


def test_yaml_overlay_members_can_be_groups():
    net = load_topology("schema: bytesim/topology-v1\n"
                        "devices: {g1: 2, s1: 3, c1: 1, c2: 1}\n"
                        "groups: {office: [s1, c1, c2]}\n"
                        "links: [{chain: [g1, s1, c1]}, {connect: [s1, c2]}]\n"
                        "overlays: {ol: {members: [office, g1], routes: [c1], lan: on}}\n")
    assert net.overlay_members("ol") == ["c1", "c2", "g1"] and net.overlays["ol"]["lan"] is True


# ---- review follow-ups ----------------------------------------------------------------

@pytest.mark.parametrize("cmd, error", [
    ("fail c8\noverlay new c1 coord=c8", "c1 can't join overlay new: coordination server c8 is down"),
    ("fail g3:1\noverlay new c2 coord=c7", "c2 can't reach coordination server c7"),
    ("fail c8\noverlay ol c2 coord=c8", "c2 can't join overlay ol: coordination server c8 is down"),
])
def test_new_members_must_reach_the_coordination_server_the_command_sets(cmd, error):
    r = runner()
    with pytest.raises(NetError, match=error):
        r.run(cmd)
    assert "new" not in r.net.overlays and r.net.overlays["ol"]["coord"] == "c7"   # nothing changed


def test_joining_with_coord_none_needs_no_server():
    r = runner()
    r.run("fail c7\noverlay ol c2 coord=none")
    assert "c2" in r.net.overlay_members("ol") and "coord" not in r.net.overlays["ol"]


def test_overlays_avoid_networks_interfaces_already_use():
    r = runner()
    r.run("ip c2 (101,5) 1")                                # a static address in the overlay range
    r.run("overlay lab c6")
    assert r.net.overlays["lab"]["net"] == 102
    with pytest.raises(NetError, match="network 101 is in use by interfaces"):
        r.run("overlay lab2 c4 net=101")
    with pytest.raises(NetError, match="network 100 is overlay ol's"):
        r.run("ip c4 (100,9)")


def test_a_save_whose_overlay_network_clashes_with_an_interface_moves_the_overlay():
    net = runner().net
    net.iface("c2", 1).mode, net.iface("c2", 1).net, net.iface("c2", 1).host = "static", 100, 77
    net.load_overlays(net.overlay_state())
    assert net.overlays["ol"]["net"] == 101 and net.ol_resolve((100, 77)) is None


@pytest.mark.parametrize("name", ["all", "off", "remove", "internet", "none", "peers"])
def test_yaml_rejects_overlay_names_the_command_reserves(name):
    with pytest.raises(NetError, match=f"overlays.{name}: '{name}' is a word the overlay command uses"):
        load_topology("schema: bytesim/topology-v1\ndevices: {c1: 1}\n"
                      f"overlays: {{'{name}': {{members: [c1]}}}}\n")


def _replies(r):
    """Each traced reply's endpoint line, ECHO REPLY line and first note, by replier and sender."""
    lines = text(r).splitlines()
    out = {}
    for i, line in enumerate(lines):
        if m := re.match(r"TRACE REPLY (\S+) → (\S+) · endpoints (.*)", line):
            echo = lines[i + 2]                               # after the hop-by-hop line
            note = next(n.strip() for n in lines[i + 3:] if not n.strip().startswith("ARP"))
            out[m[1].split("@")[0], m[2]] = (m[3], echo, note)
    return out


def test_mping_overlay_replies_report_their_own_tunnel_not_the_last_request():
    r = runner()
    r.run("trace on\nmping all all@ol")
    got = _replies(r)
    assert len(got) == 12
    for (replier, sender), (ends, echo, note) in got.items():
        here, there = r.net.ol_addr("ol", replier), r.net.ol_addr("ol", sender)
        assert ends.endswith(f"→ {sender}:nic1 {fmt(r.net.addr_of((sender, 1)))}"), (replier, sender, ends)
        assert echo.startswith(f"ECHO REPLY {fmt(here)} → {fmt(there)} ({replier} → {fmt(there)} {sender})")
        assert note.startswith(f"overlay ol: {fmt(here)} {replier} ⇢ {fmt(there)} {sender}, a reply")
        relayed = {replier, sender} == {"c3", "c5"}
        assert note.endswith("(relayed via c8)" if relayed else "(direct)"), note


def test_mping_replies_through_an_exit_node_report_their_own_endpoints():
    r = runner()
    r.run("overlay ol exit=c7\ntrace on\nmping c1 c8 c3@ol")    # c8 through the exit node, c3 tunnelled
    got = _replies(r)
    assert got["c3", "c1"][2].startswith("overlay ol: (100,2) c3 ⇢ (100,1) c1, a reply")
    for d in ("c8",):
        ends, _, note = got[d, "c1"]
        assert ends.endswith("→ c1:nic1 (7,10)") and note == "a reply: back along the request's path to c1"


@pytest.mark.parametrize("name", ["Off", "REMOVE", "Internet"])
def test_saves_drop_overlays_named_like_reserved_words_in_any_case(name):
    net = runner().net
    state = net.overlay_state()
    state[name] = dict(state["ol"], net=105)
    net.load_overlays(state)
    assert set(net.overlays) == {"ol"}


def test_a_save_loads_even_when_no_overlay_network_is_free():
    net = runner().net
    nics = [k for k in net.ifaces if net.ifaces[k].addr][:10]   # an older save: every overlay network in use
    assert len(nics) == 10
    for nic, ov_net in zip(nics, range(100, 110)):
        net.ifaces[nic] = Iface(mode="static", via="static", net=ov_net, host=1)
    back = Network.from_json(json.loads(json.dumps(net.to_json())))
    assert back.overlays == {} and back.ifaces[nics[0]].net == 100
