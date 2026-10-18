"""Construction scripts: every script must replay into an equivalent network."""
import pytest

from bytesim import demos
from bytesim.model import NetError
from bytesim.model.equivalence import differences
from bytesim.scripting import replay
from bytesim.synthesis import MODES, Limits, synthesize
from bytesim.topology import load_topology

from test_topology_format import SITES


def roundtrip(net, mode="readable", **kw):
    res = synthesize(net, mode, **kw)
    assert differences(net, replay(res.script)) == [], res.script
    return res


def build(*commands):
    return replay("\n".join(commands))


# ---- baseline reconstruction ------------------------------------------------------------
CASES = {
    "empty": [],
    "single device": ["add r 4 1"],
    "two computers": ["add c 1 2", "c1 c2"],
    "kinds and port counts": ["add g 2 1", "add r 6 2", "add s 24 1", "add s 5 2", "add c 3 1", "r1 s1", "c1:3 s2:5"],
    "nested and loose": ["add s 4 4", "add c 1 2", "group a s1 s2", "group b a s3", "group top b c1", "s1 s2", "c2 s4"],
    "disconnected": ["add s 4 2", "add c 1 2", "s1 c1", "s2 c2"],
    "parallel and cycle": ["add s 4 3", "s1 s2", "s1 s2", "s2 s3", "s3 s1"],
    "roles": ["add r 6 2", "r1:5 r2:5 role=control", "r1:6 r2:6 role=control-backup", "r1:4 r2:4 role=fabric"],
    "cluster": ["add r 4 2", "add s 4 1", "cluster core r1 r2 policy=witness", "r1 s1", "r2 s1"],
    "stp and dhcp": ["add r 4 1", "add s 4 2", "r1 s1", "s1 s2", "s2 s1", "stp s1 off", "stp priority s2 4096",
                     "dhcpd r1 off"],
    "interfaces": ["add r 4 1", "add c 2 3", "r1 c1", "ip c1:1 (5,50) 1", "ip c2:1 adhoc", "ip c3:2 none",
                   "ip r1:2 none", "ip r1:3 (9,1)"],
    "gaps in ids": ["add s 4 6", "add r 4 3", "del s2 s5 s6", "del r1", "s1 s3"],
    "failures": ["add r 4 2", "add s 4 1", "r1 s1", "r2 s1", "fail r1", "fail s1:2"],
}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("mode", MODES)
def test_baseline_cases_rebuild_exactly(name, mode):
    roundtrip(build(*CASES[name]), mode)


@pytest.mark.parametrize("make", [lambda: demos.basic()[0], lambda: demos.ha()[0], lambda: demos.loop()[0],
                                  lambda: load_topology(SITES)])
@pytest.mark.parametrize("mode", MODES)
def test_demo_networks_in_every_mode(make, mode):
    roundtrip(make(), mode)


def test_output_is_deterministic_and_never_longer_than_explicit():
    net = load_topology(SITES)
    scripts = {m: synthesize(net, m) for m in MODES}
    assert synthesize(net, "shortest").script == scripts["shortest"].script
    assert scripts["shortest"].commands <= scripts["readable"].commands <= scripts["explicit"].commands
    assert scripts["explicit"].used == []


# ---- structural reuse ---------------------------------------------------------------------
POD = ["add r 4 2", "add s 4 2", "group pod1 r1 r2 s1 s2", "r1 s1", "r2 s2", "s1 s2"]


def test_a_cloned_group_is_rebuilt_with_clone():
    net = build(*POD, "clone pod1 pod2", "clone pod1 pod3")
    res = roundtrip(net, "shortest")
    assert {s.command() for s in res.used} >= {"clone pod1 pod2", "clone pod1 pod3"}
    assert res.commands < res.explicit_commands


@pytest.mark.parametrize("twist", [
    ["r3 s3", "r4 s4", "s3 s4", "s3:4 s4:4"],            # same containment, extra cable
    ["r3 s4", "r4 s3", "s3 s4"],                         # same containment, different cabling
])
def test_different_cabling_is_not_cloned(twist):
    net = build(*POD, "add r 4 2", "add s 4 2", "group pod2 r3 r4 s3 s4", *twist)
    res = roundtrip(net, "shortest")
    assert not any(s.kind in ("clone", "copy") for s in res.used)


def test_same_cabling_different_grouping_is_not_cloned():
    net = build(*POD, "add r 4 2", "add s 4 2", "group pod2 r3 r4", "group pod3 pod2 s3 s4",
                "r3 s3", "r4 s4", "s3 s4")
    res = roundtrip(net, "shortest")
    assert not any(s.kind in ("clone", "copy") and s.src == "pod1" for s in res.used)


def test_different_ports_or_roles_are_not_cloned():
    a = build(*POD, "add r 6 2", "add s 4 2", "group pod2 r3 r4 s3 s4", "r3 s3", "r4 s4", "s3 s4")
    b = build(*POD, "add r 4 2", "add s 4 2", "group pod2 r3 r4 s3 s4", "r3 s3", "r4 s4", "s3 s4 role=fabric")
    for net in (a, b):
        assert not any(s.kind == "clone" for s in roundtrip(net, "shortest").used)


def test_ids_that_dont_follow_on_block_a_clone():
    # pod2 is pod1's twin but its switches aren't consecutive ids: clone can't make them
    net = build(*POD, "add r 4 2", "add s 4 3", "group pod2 r3 r4 s3 s5", "r3 s3", "r4 s5", "s3 s5")
    res = roundtrip(net, "shortest")
    assert not any(s.kind == "clone" for s in res.used)


def test_a_candidate_that_replays_wrong_is_rejected():
    # pod2 matches pod1 structurally, but its subgroup is called "zz": `clone` would call it "inner-2"
    net = build("add s 4 4", "group inner s1 s2", "group pod1 inner", "s1 s2",
                "group zz s3 s4", "group pod2 zz", "s3 s4")
    res = roundtrip(net, "shortest")
    assert res.rejected >= 1 and not any(s.dst == "pod2" and s.kind == "clone" for s in res.used)


def test_search_limits_fall_back_to_explicit():
    net = build(*POD, "clone pod1 pod2")
    res = roundtrip(net, "shortest", limits=Limits(max_candidates=0))
    assert res.used == [] and res.commands == res.explicit_commands


# ---- boundary remapping ---------------------------------------------------------------------
def transit():
    """link1 joins rfab1 and rfab2; link2 joins rfab2 and rfab3 the same way (ports copy made it)."""
    return build("add r 8 6", "add s 5 4", "group rfab1 r1 r2", "group rfab2 r3 r4", "group rfab3 r5 r6",
                 "group link1 s1 s2", "group link2 s3 s4", "s1 s2", "s1 s2",
                 "r1 s1", "r2 s1", "r1 s2", "r2 s2", "r3 s1", "r4 s2",
                 "ports copy link1(rfab1, rfab2) link2(rfab2, rfab3)")


def test_ports_copy_is_found_for_a_transit_fabric():
    net = transit()
    res = roundtrip(net, "shortest")
    assert "ports copy link1(rfab1,rfab2) link2(rfab2,rfab3)" in [s.command() for s in res.used]


def test_copy_with_rewiring_is_found_when_the_group_is_a_clone_too():
    net = build("add r 8 6", "add s 5 2", "group rfab1 r1 r2", "group rfab2 r3 r4", "group rfab3 r5 r6",
                "group link1 s1 s2", "s1 s2", "s1 s2", "r1 s1", "r2 s1", "r1 s2", "r2 s2", "r3 s1", "r4 s2",
                "copy link1(rfab1, rfab2) link2(rfab2, rfab3)")
    res = roundtrip(net, "shortest")
    assert any(s.kind == "copy" and s.dst == "link2" for s in res.used)


def test_partial_boundaries_and_moved_ports_still_rebuild():
    net = transit()
    net.disconnect("s3", None, "r5")                     # partial: one cable no longer follows the pattern
    roundtrip(net, "shortest")
    net2 = transit()
    net2.connect("r4", 8, "r6", 8)                       # occupies a port the pattern would have preferred
    roundtrip(net2, "shortest")


# ---- limits of the language ---------------------------------------------------------------
def test_inexpressible_settings_are_reported_not_hidden():
    net = build("add r 4 2", "add s 4 1", "group pair r1 r2", "group wrap pair s1", "r1 s1")
    net.clusters["wrap"] = "quorum"                      # no router directly in wrap: `cluster` can't mark it
    res = synthesize(net, "explicit")
    assert any("cluster wrap" in t for t in res.limitations)
    assert "NOT EXPRESSIBLE: cluster wrap" in res.script


def test_explicit_failure_is_an_error_not_a_bad_script(monkeypatch):
    import bytesim.synthesis as syn
    monkeypatch.setattr(syn, "verify", lambda *a, **k: ["something differs"])
    with pytest.raises(NetError, match="couldn't build a script"):
        synthesize(build("add s 4 1"), "explicit")


def test_scripts_run_with_comments_and_report_the_failing_line():
    net = replay("# a comment\n\nadd s 4 2   # trailing comment\ns1 s2\n")
    assert len(net.links) == 1
    with pytest.raises(NetError, match="line 2 .*no such device"):
        replay("add s 4 1\ns1 s9\n")
