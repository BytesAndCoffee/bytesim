from collections import Counter

import pytest

from bytesim import demos
from bytesim.model import NetError, Network
from bytesim.topology import dump_topology, load_topology, looks_like_topology

SITES = """
schema: bytesim/topology-v1
defaults: {stp: on, interfaces: {computer: dhcp}}
templates:
  site:
    devices:
      routers: {kind: router, count: 2, ports: 6}
      dist:    {kind: switch, count: 2, ports: 6}
      access:  {kind: switch, count: 2, ports: 4}
      pcs:     {kind: computer, count: 2}
    groups:
      core: {cluster: witness, members: [routers]}
      fabric: dist
      office:
        room1: ["access[0]", "pcs[0]"]
        room2: ["access[1]", "pcs[1]"]
    links:
      - mesh: [routers, dist]
      - mesh: [dist, access]
      - connect: ["access[0]", "access[1]"]
      - zip: [pcs, access]
      - {connect: ["routers[0]:6", "routers[1]:6"], role: control}
instances:
  north:
    template: site
    devices: {routers: [r1, r2]}
    groups: {core: rcore1, office.room1: lab}
  south:
    template: site
devices: {s99: {ports: 4, stp: off}}
groups:
  transit: [s99]
links:
  - mesh: [[r1, r3], s99]
"""


def pairs(net):
    return Counter(tuple(sorted((l.a, l.b))) for l in net.links)


def test_templates_expand_into_devices_groups_and_cables():
    net = load_topology(SITES)
    # 2 sites × 8 devices + s99; fixed ids are honoured, the rest numbered around them
    assert len(net.devices) == 17 and {"r1", "r2", "r3", "r4", "s99"} <= set(net.devices)
    assert len(net.links) == 2 * (4 + 4 + 1 + 2 + 1) + 2
    assert [l.role for l in net.links].count("control") == 2
    assert net.groups["north"] == ["rcore1", "fabric", "office"]
    assert net.groups["south"] == ["core", "fabric-2", "office-2"]       # free names first, then -2
    assert net.groups["office"] == ["lab", "room2"]
    assert net.clusters == {"rcore1": "witness", "core": "witness"}
    assert net.stp_off == {"s99"}
    assert all(net.ifaces[f"{c}:1"].mode == "dhcp" for c in net.devices if c.startswith("c"))


def test_pinned_ports_and_file_order_allocation():
    net = load_topology(SITES)
    l = next(l for l in net.links if l.role == "control" and l.a == "r1")
    assert (l.pa, l.pb) == (6, 6)
    assert net.link_on("r1", 1).other("r1")[0] == "s1"   # the first mesh cable takes port 1
    assert sorted(d for d in net.devices if d[0] == "s") == sorted(
        [f"s{i}" for i in range(1, 9)] + ["s99"], key=lambda d: int(d[1:]))     # lowest free numbers


@pytest.mark.parametrize("bad, message", [
    ("instances: {a: {template: nope}}", "unknown template"),
    ("templates: {t: {devices: {x: {kind: router}}, links: [{mesh: [x, y]}]}}\ninstances: {a: {template: t}}",
     "no device, role or group|not a device, role or group"),
    ("templates: {t: {devices: {x: {kind: switch, count: 2}, y: {kind: switch}}, links: [{zip: [x, y]}]}}\n"
     "instances: {a: {template: t}}", "zip needs equal sizes"),
    ("devices: {r1: 4, s1: 4, s2: 4}\nlinks: [{connect: [r1, [s1, s2]]}]", "connect joins single devices"),
    ("templates: {t: {devices: {x: {kind: switch}}, groups: {a: {f: x}, b: {f: x}}}}\n"
     "instances: {i: {template: t, groups: {f: z}}}", "ambiguous"),
    ("templates: {t: {devices: {x: {kind: switch}}}}\ninstances: {i: {template: t, devices: {x: [r7]}}}",
     "named like a router"),
    ("devices: {s1: 4}\ntemplates: {t: {devices: {x: {kind: switch}}}}\n"
     "instances: {i: {template: t, devices: {x: [s1]}}}", "declared twice"),
    ("devices: {s1: 1, c1: 1, c2: 1}\nlinks: [{connect: [s1, c1]}, {connect: [s1, c2]}]", "no free ports"),
])
def test_errors_say_where(bad, message):
    with pytest.raises(NetError, match=message):
        load_topology("schema: bytesim/topology-v1\n" + bad)


@pytest.mark.parametrize("build", [demos.basic, demos.ha, demos.loop])
def test_export_round_trips(build):
    net, _ = build()
    net.stp_priority["s1"] = 4096
    net.dhcp_off |= {d for d in net.devices if d.startswith("r")}
    compact = load_topology(dump_topology(net))
    assert pairs(compact) == pairs(net)
    assert {g: set(m) for g, m in compact.groups.items()} == {g: set(m) for g, m in net.groups.items()}
    assert compact.clusters == net.clusters and compact.stp_off == net.stp_off
    assert compact.stp_priority == net.stp_priority and compact.dhcp_off == net.dhcp_off
    exact = load_topology(dump_topology(net, pin_ports=True))
    key = lambda n: sorted((l.a, l.pa, l.b, l.pb, l.role) for l in n.links)   # noqa: E731
    assert key(exact) == key(net)


def test_compact_export_folds_meshes():
    text = dump_topology(load_topology(SITES))
    # routers × fabric and the two transit uplinks are complete meshes between groups
    assert "- mesh: [rcore1, fabric]" in text and "- mesh: [core, fabric-2]" in text
    assert len(text.splitlines()) < len(dump_topology(load_topology(SITES), pin_ports=True).splitlines())


def test_save_files_and_topologies_are_told_apart():
    net, _ = demos.basic()
    assert not looks_like_topology(net.to_json())
    assert looks_like_topology(dump_topology(net))
    assert looks_like_topology(SITES)
    assert Network.from_json(net.to_json()).devices.keys() == net.devices.keys()


AUTO = """
schema: bytesim/topology-v1
templates:
  pod:
    devices:
      core: {kind: router, count: 2}
      leaf: {kind: switch, count: 3}
    groups:
      spine: core
      leaves: leaf
    links:
      - mesh: [core, leaf]
instances:
  pod1:
    template: pod
    devices: {core: [r1, r2], leaf: [s1, s2, s3]}
    groups: {spine: fab1, leaves: fab2}
  pod2:
    template: pod
    group: pod(1)
    devices: {core: [r(2)], leaf: [s9, s(2)]}
    groups: {spine: fab(1), leaves: fab(1)}
devices: {s50: 4}
groups:
  spare(1): [s50]
"""


def test_auto_numbered_ids_and_group_names():
    net = load_topology(AUTO)
    # r(2) skips the fixed r1/r2; s(2) skips s1–s3 and the fixed s9
    assert net.groups["fab3"] == ["r3", "r4"] and net.groups["fab4"] == ["s9", "s4", "s5"]
    # pod(1): pod1 is taken by the first instance's wrapper, so the second becomes pod2
    assert net.groups["pod2"] == ["fab3", "fab4"]
    assert net.groups["spare1"] == ["s50"]


def test_a_device_belongs_to_one_group():
    with pytest.raises(NetError, match="r1 is already in group fab1"):
        load_topology(AUTO.replace("spare(1): [s50]", "spare(1): [r1]"))
    with pytest.raises(NetError, match="already in group"):
        load_topology("devices: {s1: 4}\ngroups: {a: [s1, s1]}")


@pytest.mark.parametrize("change, message", [
    (("[r(2)]", "[s(2)]"), "holds routers, so write r"),
    (("[s9, s(2)]", "[s9, s(3)]"), "the template has 3, you gave 4"),
    (("fab(1), leaves", "fab(2), leaves"), "stands for one group"),
    (("group: pod(1)", "group: s(1)"), "names like device ids"),
])
def test_auto_number_mistakes(change, message):
    with pytest.raises(NetError, match=message):
        load_topology(AUTO.replace(*change))


def test_console_accepts_auto_names():
    net, _ = demos.ha()
    assert net.resolve_group_name("fab(1)") == "fab3"
    assert net.resolve_group_name("lab") == "lab"


ORDER = """
schema: bytesim/topology-v1
templates:
  t:
    devices: {sw: {kind: switch, count: 3}}
    groups: {fab: sw}
instances:
  a:
    template: t
    devices: {sw: [s(3)]}               # allocated first in the file…
    groups: {fab: fab(1)}
  b:
    template: t
    devices: {sw: [s5, s8, s9]}         # …but these explicit ids come later
    groups: {fab: fab1}                 # and so does this explicit group name
devices: {s1: 4, s2: 4, s4: 4, s7: 4}
groups:
  link1: [s1, s2]
  office: [s4, s7]
"""


def test_explicit_names_win_regardless_of_order():
    net = load_topology(ORDER)
    # s1 s2 s4 s7 exist, s5 s8 s9 are claimed later: s(3) fills the gaps that are really free
    assert net.groups["fab2"] == ["s3", "s6", "s10"]
    assert net.groups["fab1"] == ["s5", "s8", "s9"]          # the explicit fab1 was kept free
    assert net.groups["link1"] == ["s1", "s2"]


def test_a_template_name_steps_aside_for_a_later_explicit_one():
    # instance a leaves its template group unnamed ("fab"); a stand-alone group fixes "fab" later
    text = ORDER.replace("    groups: {fab: fab(1)}\n", "").replace("  office: [s4, s7]", "  fab: [s4, s7]")
    net = load_topology(text)
    assert net.groups["fab"] == ["s4", "s7"] and net.groups["fab-2"] == ["s3", "s6", "s10"]


def test_auto_ids_only_depend_on_the_file():
    # The same file always expands the same way: allocation starts from an empty network.
    assert load_topology(ORDER).to_json() == load_topology(ORDER).to_json()
