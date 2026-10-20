"""Layout geometry, physical topology, and snapshot compatibility."""
import json
from itertools import combinations

import pytest

from bytesim import Network, demos
from bytesim.constants import BOX_H
from bytesim.model import NetError
from bytesim.model.equivalence import differences
from bytesim.model.layout import LAYOUT_MODES


def positions(net):
    return {d.id: (d.x, d.y) for d in net.devices.values()}


def bounds(net, member):
    if member in net.groups:
        return net.group_bounds(member)
    d = net.devices[member]
    return d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1


def overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


@pytest.mark.parametrize("mode", LAYOUT_MODES)
@pytest.mark.parametrize("demo", demos.DEMOS)
@pytest.mark.parametrize("orientation", ("auto", "horizontal", "vertical"))
def test_layout_preserves_network_and_separates_nested_frames(mode, demo, orientation):
    net, _ = demos.DEMOS[demo]()
    tops = [g for g in net.groups if net.parent_of(g) is None]
    if tops:
        net.group_add("site", tops)
        net.group_add("campus", ["site"])
    original = Network.from_json(net.to_json())
    net.auto_layout(100, mode, orientation)
    assert not differences(original, net)
    for a, b in combinations(net.devices, 2):
        assert not overlaps(bounds(net, a), bounds(net, b)), (a, b)
    sibling_sets = [ms for ms in net.groups.values()]
    sibling_sets.append([m for m in list(net.devices) + list(net.groups)
                         if net.parent_of(m) is None])
    for siblings in sibling_sets:
        for a, b in combinations(siblings, 2):
            assert not overlaps(bounds(net, a), bounds(net, b)), (a, b)
    for g in net.groups:
        outer = bounds(net, g)
        for m in net.groups[g]:
            inner = bounds(net, m)
            assert outer[0] < inner[0] <= inner[2] < outer[2]
            assert outer[1] < inner[1] <= inner[3] < outer[3]
    first = positions(net)
    # Layout must not depend on where a user previously dragged devices.
    for d in net.devices.values():
        d.x, d.y = 1000 - d.num * 13, -1000 + d.num * 7
    net.auto_layout(100)
    assert positions(net) == first


def test_tree_follows_cable_depth_instead_of_device_kind():
    net = Network()
    for kind in "rcs":
        net.add(kind, 4)
    net.connect("r1", None, "c1", None)
    link = net.connect("c1", None, "s1", None, role="control")
    net.auto_layout(100)
    assert net.devices["s1"].y < net.devices["c1"].y
    net.auto_layout(100, "tree")
    assert net.devices["r1"].y < net.devices["c1"].y < net.devices["s1"].y
    before = positions(net)
    link.up = False
    net.devices["c1"].up = False
    net.auto_layout(100)
    assert positions(net) == before


@pytest.mark.parametrize("mode", ("grid", "compact"))
def test_packed_layout_wraps_to_width_with_large_devices_and_disconnected_components(mode):
    net = Network()
    for ports in (64, 1, 2, 8, 1, 64):
        net.add("s", ports)
    net.auto_layout(35, mode)
    assert len({d.y for d in net.devices.values()}) > 1
    for a, b in combinations(net.devices, 2):
        assert not overlaps(bounds(net, a), bounds(net, b))
    for d in net.devices.values():
        assert d.x >= 2 and d.y >= 1
        assert d.width > 35 or d.x + d.width <= 37


def test_circular_places_a_ring_on_both_axes():
    net = Network()
    for _ in range(4):
        net.add("s")
    net.auto_layout(100, "circular")
    top, right, bottom, left = (net.devices[f"s{i}"] for i in range(1, 5))
    assert top.x == bottom.x and right.y == left.y
    assert top.y < right.y < bottom.y and left.x < top.x < right.x


@pytest.mark.parametrize("mode", LAYOUT_MODES)
def test_layout_mode_and_positions_survive_snapshot_roundtrip(mode):
    net, _ = demos.basic()
    net.auto_layout(100, mode)
    restored = Network.from_json(net.to_json())
    assert restored.layout_mode == mode
    assert positions(restored) == positions(net)
    restored.auto_layout(100)
    assert positions(restored) == positions(net)


def test_older_snapshots_default_to_layered_and_bad_modes_are_rejected():
    data = json.loads(Network().to_json())
    del data["layout_mode"]
    net = Network.from_json(json.dumps(data))
    assert net.layout_mode == "layered"
    with pytest.raises(NetError, match="layout must be"):
        net.auto_layout(100, "unknown")
    assert net.layout_mode == "layered"
    data["layout_mode"] = "unknown"
    with pytest.raises(NetError, match="layout must be"):
        Network.from_json(json.dumps(data))


@pytest.mark.parametrize("mode", LAYOUT_MODES)
def test_empty_and_single_device_layouts(mode):
    net = Network()
    net.auto_layout(1, mode)
    assert net.layout_mode == mode
    net.add("c")
    net.auto_layout(1)
    assert net.devices["c1"].x >= 0 and net.devices["c1"].y >= 0


@pytest.mark.parametrize("mode", LAYOUT_MODES)
@pytest.mark.parametrize("orientation", ("horizontal", "vertical"))
def test_group_orientation_keeps_nested_blocks_separate(mode, orientation):
    net, _ = demos.ha()
    net.group_add("site", ["fab2", "rfab1", "fab1"])
    net.set_group_layout("site", orientation)
    net.set_group_layout("fab1", "vertical")
    net.set_group_layout("fab2", "horizontal")
    net.auto_layout(80, mode)
    for a, b in combinations(net.devices, 2):
        assert not overlaps(bounds(net, a), bounds(net, b))
    for group in net.groups:
        for a, b in combinations(net.groups[group], 2):
            assert not overlaps(bounds(net, a), bounds(net, b))
    first = positions(net)
    net.auto_layout(80)
    assert positions(net) == first


def test_group_orientation_is_saved_cloned_reset_and_pruned():
    net, _ = demos.ha()
    net.set_group_layout("fab1", "vertical")
    net.set_group_layout("rfab1", "horizontal")
    net.group_add("site", ["fab1", "rfab1"])
    net.set_group_layout("site", "vertical")
    restored = Network.from_json(net.to_json())
    assert restored.group_layouts == net.group_layouts
    clone = net.clone_group("site")
    for old, new in clone.groups.items():
        assert net.group_layouts[new] == net.group_layouts[old]
    net.set_group_layout("site", "auto")
    assert "site" not in net.group_layouts
    net.ungroup("fab1")
    assert "fab1" not in net.group_layouts
    for d in list(net.devices_in(clone.group)):
        net.remove(d)
    assert not set(clone.groups.values()) & net.group_layouts.keys()
    before = dict(net.group_layouts)
    with pytest.raises(NetError, match="group layout must be"):
        net.set_group_layout("rfab1", "diagonal")
    assert net.group_layouts == before


def test_group_orientation_commands_work_in_headless_scripts():
    from bytesim.scripting import replay

    net = replay("add s 8 3\ngroup fab s3 s1 s2 layout=vertical\nlayout grid\n")
    net.auto_layout(100)
    assert net.group_layouts == {"fab": "vertical"} and net.layout_mode == "grid"
    assert net.devices["s3"].y < net.devices["s1"].y < net.devices["s2"].y


@pytest.mark.parametrize("mode", LAYOUT_MODES)
def test_default_orientation_applies_to_groups_with_explicit_overrides(mode):
    net, _ = demos.ha()
    net.set_group_layout("fab2", "horizontal")
    net.auto_layout(100, mode, "vertical")
    def relative(a, b):
        first, second = net.devices[a], net.devices[b]
        return second.x - first.x, second.y - first.y

    assert relative("s1", "s2") != relative("s3", "s4")
    net.set_group_layout("fab2", "auto")
    net.auto_layout(100)
    assert relative("s1", "s2") == relative("s3", "s4")
    restored = Network.from_json(net.to_json())
    assert restored.group_orientation == "vertical"
    assert positions(restored) == positions(net)
    net.auto_layout(100, orientation="auto")
    assert net.group_orientation == "auto" and net.layout_mode == mode


@pytest.mark.parametrize("command,mode,orientation", [
    ("layout grid vertical", "grid", "vertical"),
    ("layout vertical grid", "grid", "vertical"),
    ("layout horizontal tree", "tree", "horizontal"),
    ("layout circular", "circular", "horizontal"),
    ("layout compact vertical", "compact", "vertical"),
    ("layout horizontal compact", "compact", "horizontal"),
    ("layout compact-layered vertical", "compact-layered", "vertical"),
    ("layout horizontal compact-layered", "compact-layered", "horizontal"),
    ("layout vertical", "tree", "vertical"),
    ("layout auto", "tree", "auto"),
    ("layout", "tree", "horizontal"),
    ("auto grid vertical", "grid", "vertical"),
    ("arrange VERTICAL GRID", "grid", "vertical"),
])
def test_layout_arguments_dispatch_by_membership(command, mode, orientation):
    from bytesim.scripting import ScriptRunner

    runner = ScriptRunner()
    runner.run("layout tree horizontal\n" + command)
    assert runner.net.layout_mode == mode
    assert runner.net.group_orientation == orientation


@pytest.mark.parametrize("args", ["grid tree", "vertical horizontal", "grid grid", "vertical vertical",
                                  "grid diagonal", "vertical nope", "list grid", "grid vertical extra"])
def test_invalid_layout_arguments_do_not_partially_update_settings(args):
    from bytesim.scripting import ScriptRunner

    runner = ScriptRunner()
    runner.run("layout circular horizontal")
    before = runner.net.to_json()
    with pytest.raises(NetError, match="usage: layout"):
        runner.run("layout " + args)
    assert runner.net.to_json() == before


def test_default_orientation_validation_and_legacy_snapshots():
    net = Network()
    with pytest.raises(NetError, match="orientation must be"):
        net.auto_layout(100, "grid", "diagonal")
    assert net.layout_mode == "layered" and net.group_orientation == "auto"
    data = json.loads(net.to_json())
    del data["group_orientation"]
    assert Network.from_json(json.dumps(data)).group_orientation == "auto"
    data["group_orientation"] = "diagonal"
    with pytest.raises(NetError, match="orientation must be"):
        Network.from_json(json.dumps(data))


@pytest.mark.parametrize("demo", demos.DEMOS)
def test_compact_reduces_demo_footprint_compared_with_grid(demo):
    net, _ = demos.DEMOS[demo]()
    roots = set((net.chain(d.id) or [d.id])[0] for d in net.sorted_devices())

    def area():
        boxes = [bounds(net, m) for m in roots]
        return ((max(b[2] for b in boxes) - min(b[0] for b in boxes) + 1)
                * (max(b[3] for b in boxes) - min(b[1] for b in boxes) + 1))

    net.auto_layout(100, "grid")
    grid_area = area()
    net.auto_layout(100, "compact")
    assert area() < grid_area


def test_compact_fills_space_in_earlier_rows():
    net = Network()
    net.add("s", 14)
    net.add("s", 14)
    net.add("s", 1)
    net.auto_layout(30, "compact")
    assert net.devices["s1"].y == net.devices["s3"].y < net.devices["s2"].y
    assert net.devices["s3"].x > net.devices["s1"].x + net.devices["s1"].width


@pytest.mark.parametrize("mode", ("layered", "tree", "compact-layered"))
@pytest.mark.parametrize("orientation", ("horizontal", "vertical"))
@pytest.mark.parametrize("grouped", (False, True))
def test_orientation_preserves_levels_and_branching(mode, orientation, grouped):
    net = Network()
    for kind in "rsscc":
        net.add(kind, 4)
    for a, b in [("r1", "s1"), ("r1", "s2"), ("s1", "c1"), ("s2", "c2")]:
        net.connect(a, None, b, None)
    if grouped:
        net.group_add("lab", list(net.devices))
        net.set_group_layout("lab", orientation)
        net.auto_layout(80, mode)
    else:
        net.auto_layout(80, mode, orientation)
    axis = 0 if orientation == "horizontal" else 1
    cross = 1 - axis
    pts = positions(net)
    assert pts["r1"][axis] < pts["s1"][axis] < pts["c1"][axis]
    assert pts["r1"][axis] < pts["s2"][axis] < pts["c2"][axis]
    assert pts["s1"][axis] == pts["s2"][axis]
    assert pts["s1"][cross] != pts["s2"][cross]


@pytest.mark.parametrize("mode", ("grid", "compact"))
@pytest.mark.parametrize("orientation", ("horizontal", "vertical"))
@pytest.mark.parametrize("grouped", (False, True))
def test_packed_orientation_still_wraps_in_two_dimensions(mode, orientation, grouped):
    net = Network()
    for _ in range(16):
        net.add("s", 4)
    if grouped:
        net.group_add("lab", list(net.devices))
        net.set_group_layout("lab", orientation)
        net.auto_layout(35, mode, height=24)
    else:
        net.auto_layout(35, mode, orientation, height=24)
    assert len({d.x for d in net.devices.values()}) > 1
    assert len({d.y for d in net.devices.values()}) > 1
    boxes = [bounds(net, d) for d in net.devices]
    if orientation == "horizontal":
        assert max(b[2] for b in boxes) - min(b[0] for b in boxes) + 1 <= 35
    else:
        assert max(b[3] for b in boxes) - min(b[1] for b in boxes) + 1 <= 24


@pytest.mark.parametrize("orientation", ("horizontal", "vertical"))
@pytest.mark.parametrize("grouped", (False, True))
def test_circular_orientation_keeps_a_ring(orientation, grouped):
    net = Network()
    for _ in range(4):
        net.add("s", 4)
    if grouped:
        net.group_add("lab", list(net.devices))
        net.set_group_layout("lab", orientation)
        net.auto_layout(80, "circular")
    else:
        net.auto_layout(80, "circular", orientation)
    assert len({d.x for d in net.devices.values()}) == 3
    assert len({d.y for d in net.devices.values()}) == 3


@pytest.mark.parametrize("demo", demos.DEMOS)
@pytest.mark.parametrize("orientation", ("auto", "horizontal", "vertical"))
def test_compact_layered_reduces_space_without_changing_layer_order(demo, orientation):
    net, _ = demos.DEMOS[demo]()
    # Explicit orientation uses the same recursive group layout in both modes.
    net.auto_layout(100, "layered", "vertical" if orientation == "auto" else orientation)
    axis = 0 if orientation == "horizontal" else 1
    before = positions(net)
    roots = set((net.chain(d.id) or [d.id])[0] for d in net.sorted_devices())

    def area():
        boxes = [bounds(net, m) for m in roots]
        return ((max(b[2] for b in boxes) - min(b[0] for b in boxes) + 1)
                * (max(b[3] for b in boxes) - min(b[1] for b in boxes) + 1))

    normal_area = area()
    net.auto_layout(100, "compact-layered", orientation)
    after = positions(net)
    for a, b in combinations(net.devices, 2):
        # Compare depth order only within one frame: parents center whole blocks.
        if net.parent_of(a) == net.parent_of(b):
            assert ((before[a][axis] > before[b][axis]) - (before[a][axis] < before[b][axis])
                    == (after[a][axis] > after[b][axis]) - (after[a][axis] < after[b][axis]))
    assert area() < normal_area
