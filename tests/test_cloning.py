import pytest

from bytesim import demos
from bytesim.model import NetError


def test_clone_copies_devices_internal_cables_and_tree(ha):
    ha.group_add("dc1", ["fab1", "rfab1"])
    before = len(ha.devices)
    res = ha.clone_group("dc1")
    # fab2 is taken, so fab1's copy becomes fab3
    assert res.groups == {"dc1": "dc2", "fab1": "fab3", "rfab1": "rfab2"}
    assert ha.groups["dc2"] == ["fab3", "rfab2"]
    assert len(ha.devices) == before + 4
    # internal: s1–s2, r1–s1, r2–s2 and the three r1–r2 links; everything to fab2 or computers stays behind
    assert res.cables == 6 and res.skipped > 0
    roles = sorted(l.role for l in ha.links if l.a in res.devices.values() and l.b in res.devices.values())
    assert roles == ["control", "control-backup", "data", "data", "data", "fabric"]
    assert all(len(set(ha.groups[g])) == len(ha.groups[g]) for g in ha.groups)


def test_clone_names_skip_taken_ones_and_respect_an_explicit_name(ha):
    # fab2 exists already, so a copy of fab1 becomes fab3
    assert ha.clone_group("fab1").group == "fab3"
    assert ha.clone_group("rfab1", "edge").group == "edge"
    with pytest.raises(NetError, match="already exists"):
        ha.clone_group("fab1", "fab2")
    with pytest.raises(NetError, match="device id"):
        ha.clone_group("fab1", "s9")


def test_clone_keeps_cluster_policy_and_settings_not_addresses(ha):
    ha.clusters["rfab1"] = "witness"
    ha.stp_off.add("s1")
    ha.stp_priority["s2"] = 4096
    ha.dhcp_off.add("r2")
    res = ha.clone_group("rfab1")
    assert ha.clusters[res.group] == "witness"
    sw = ha.clone_group("fab1")
    assert sw.devices["s1"] in ha.stp_off and ha.stp_priority[sw.devices["s2"]] == 4096
    assert res.devices["r2"] in ha.dhcp_off
    assert all(ha.ifaces[f"{n}:{p}"].addr is None
               for n in res.devices.values() for p in range(1, 7) if f"{n}:{p}" in ha.ifaces)


def test_clone_lands_in_the_same_parent_and_shifts_positions(ha):
    ha.group_add("site", ["fab1", "fab2"])
    x = ha.devices["s1"].x
    res = ha.clone_group("fab1", dx=40)
    assert ha.groups["site"] == ["fab1", res.group, "fab2"]
    assert ha.devices[res.devices["s1"]].x == x + 40


def test_a_cloned_loop_storms_on_its_own():
    net, _ = demos.loop()
    net.reconcile()
    res = net.clone_group("ring")              # s1–s2–s3 copied, STP still off on the copies
    net.reconcile()
    copies = set(res.devices.values())
    assert copies <= net.stp_off and len(net._areas(net._trunks(False))) == 2
    fl = net.flood((res.devices["s1"], 2))     # the copy of s1:p2, cabled to the copy of s2
    assert fl.storm and fl.storm_switches == copies
    net.start_storm(fl)
    assert net.storming == copies              # the original ring is untouched
    assert not net.flood(("c1", 1)).storm or "s1" not in net.storming


def _transit():
    """Three router pairs; transit t (s1+s2, two parallel cables) joins A and B."""
    from bytesim.model import Network
    net = Network()
    for _ in range(6):
        net.add("r", 8)
    net.add("s", 5); net.add("s", 5)
    net.group_add("a", ["r1", "r2"]); net.group_add("b", ["r3", "r4"]); net.group_add("c", ["r5", "r6"])
    net.group_add("t", ["s1", "s2"])
    net.connect("s1", None, "s2", None); net.connect("s1", None, "s2", None)
    for r in ("r1", "r2"):
        for s in ("s1", "s2"):
            net.connect(r, 6 if s == "s1" else 7, s, None)
    net.connect("r3", 6, "s1", None); net.connect("r4", 6, "s2", None)
    return net


def test_copy_rewires_outside_cables_onto_new_endpoints():
    net = _transit()
    res = net.clone_group("t", "t2", rewire=[("a", "b"), ("b", "c")])
    s3, s4 = res.devices["s1"], res.devices["s2"]
    far = sorted((l.other(s)[0]) for l in net.links for s in (s3, s4) if l.touches(s)
                 and l.other(s)[0] not in (s3, s4))
    assert far == ["r3", "r3", "r4", "r4", "r5", "r6"] and res.rewired == 6 and res.skipped == 0
    assert res.cables == 2                    # the parallel pair inside the group


def test_ports_copy_cables_an_existing_group_like_another():
    net = _transit()
    net.add("s", 5); net.add("s", 5)
    net.group_add("t2", ["s3", "s4"])
    res = net.copy_cabling("t", "t2", [("a", "b"), ("b", "c")])
    # t: s1–r1 r2 r3, s2–r1 r2 r4  →  t2: s3–r3 r4 r5, s4–r3 r4 r6; t's ports mirrored on t2
    t_ports = sorted(l.port_of("s1") for l in net.links if l.touches("s1") and "s2" not in (l.a, l.b))
    assert sorted(l.other("s3")[0] for l in net.links if l.touches("s3")) == ["r3", "r4", "r5"]
    assert sorted(l.other("s4")[0] for l in net.links if l.touches("s4")) == ["r3", "r4", "r6"]
    assert sorted(l.port_of("s3") for l in net.links if l.touches("s3")) == t_ports
    assert res.made == 6 and not any(l for l in net.links if {l.a, l.b} == {"s3", "s4"})   # no internal copy
    assert net.copy_cabling("t", "t2", [("a", "b"), ("b", "c")]).made == 0                    # idempotent


def test_ports_copy_checks_shapes_and_room():
    net = _transit()
    net.add("s", 5)
    net.group_add("small", ["s3"])
    with pytest.raises(NetError, match="matched one to one"):
        net.copy_cabling("t", "small", [("a", "b")])
    net.add("s", 5)
    net.group_add("small", ["s4"])
    with pytest.raises(NetError, match="r1 is a router but s3 is a switch"):
        net.copy_cabling("t", "small", [("a", "small")])
    for p in range(1, 9):                     # leave r5 with no room
        if net.link_on("r5", p) is None:
            net.add("c"); net.connect("r5", p, f"c{net.counters['c']}", None)
    before = net.to_json()
    with pytest.raises(NetError, match="r5 needs 1 free port"):
        net.copy_cabling("t", "small", [("b", "c")])
    assert net.to_json() == before


def test_endpoints_unlisted_are_dropped_and_mistakes_change_nothing():
    net = _transit()
    res = net.clone_group("t", "t2", rewire=[("b", "c")])
    assert res.rewired == 2 and res.skipped == 4
    before = net.to_json()
    with pytest.raises(NetError, match="matched one to one"):
        net.clone_group("t", "t3", rewire=[("a", "r5")])
    with pytest.raises(NetError, match="inside t"):
        net.clone_group("t", "t3", rewire=[("a", "t")])
    with pytest.raises(NetError, match="listed twice"):
        net.clone_group("t", "t3", rewire=[("a", "b"), ("r1", "r5")])
    for p in range(1, 9):                     # fill r5 so the copy can't fit
        if net.link_on("r5", p) is None:
            net.add("c"); net.connect("r5", p, f"c{net.counters['c']}", None)
    before = net.to_json()
    with pytest.raises(NetError, match="needs 1 free port"):
        net.clone_group("t", "t3", rewire=[("b", "c")])
    assert net.to_json() == before            # checked before anything was created
