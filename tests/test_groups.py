import pytest

from bytesim.constants import BOX_H
from bytesim.model import NetError


def test_nested_groups_and_cycle_protection(ha):
    ha.group_add("dc1", ["fab1", "rfab1"])
    ha.group_add("site", ["dc1", "fab2"])
    assert ha.chain("r1") == ["site", "dc1", "rfab1"]
    assert sorted(ha.devices_in("dc1")) == ["r1", "r2", "s1", "s2"]
    with pytest.raises(NetError, match="already inside"):
        ha.group_add("fab1", ["site"])
    with pytest.raises(NetError, match="inside itself"):
        ha.group_add("dc1", ["dc1"])


def test_dissolving_moves_members_up(ha):
    ha.group_add("dc1", ["fab1", "rfab1"])
    ha.group_add("site", ["dc1", "fab2"])
    ha.ungroup("dc1")
    assert ha.groups["site"] == ["fab1", "rfab1", "fab2"]
    assert ha.clusters == {"rfab1": "none"}


def test_deleting_the_last_member_prunes_the_group(basic):
    basic.remove("r1")
    basic.remove("r2")
    assert "core" not in basic.groups


def _overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def test_layout_has_no_overlapping_boxes_or_sibling_frames(ha):
    ha.group_add("dc1", ["fab1", "rfab1"])
    ha.group_add("site", ["dc1", "fab2"])
    ha.auto_layout(140)
    boxes = [(d.x, d.y, d.x + d.width - 1, d.y + BOX_H - 1) for d in ha.devices.values()]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _overlap(a, b)
    assert not _overlap(ha.group_bounds("dc1"), ha.group_bounds("fab2"))
    # nested frames sit strictly inside their parent
    outer, inner = ha.group_bounds("site"), ha.group_bounds("dc1")
    assert outer[0] < inner[0] and inner[2] < outer[2]


def test_direct_members_share_a_row(basic):
    assert len({basic.devices[d].y for d in basic.groups["fab1"]}) == 1
