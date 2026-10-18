"""Ready-made networks. Each builder returns (network, hint for the console)."""
from __future__ import annotations

from typing import Callable

from .model import Network


def basic() -> tuple[Network, str]:
    """A gateway, two routers, three switches and ten computers (two of them an ad-hoc pair)."""
    net = Network()
    net.add("g", 2)
    for _ in range(2):
        net.add("r", 4)
    for _ in range(3):
        net.add("s", 6)
    for i in range(10):
        net.add("c", 2 if i == 2 else 1)
    for a, b in [("g1", "r1"), ("r1", "r2"), ("r1", "s1"), ("r1", "s2"), ("r2", "s3"),
                 ("s1", "c1"), ("s1", "c2"), ("s1", "c3"), ("s2", "c3"), ("s2", "c4"),
                 ("s2", "c5"), ("s3", "c6"), ("s3", "c7"), ("s3", "c8"),
                 ("c9", "c10")]:
        net.connect(a, None, b, None)
    net.group_add("core", ["r1", "r2"])
    net.group_add("fab1", ["s1", "s2", "s3"])
    return net, ("[green]demo network loaded[/] — try [cyan]ping c1 c8[/], [cyan]mcast c1 c4 c6 c8[/], "
                 "[cyan]dhcp c4[/], [cyan]ip[/], or [cyan]ping c9 c10[/] (an ad-hoc pair, no router)")


def ha() -> tuple[Network, str]:
    """Two fabrics joined by a clustered router pair with redundant control links."""
    net = Network()
    for _ in range(2):
        net.add("r", 6)
    for _ in range(4):
        net.add("s", 6)
    for _ in range(6):
        net.add("c", 1)
    for a, pa, b in [("s1", None, "s2"), ("s3", None, "s4"), ("r1", 1, "s1"), ("r2", 1, "s2"),
                     ("r1", 2, "s3"), ("r2", 2, "s4"),
                     ("s1", None, "c1"), ("s1", None, "c2"), ("s2", None, "c3"),
                     ("s3", None, "c4"), ("s3", None, "c5"), ("s4", None, "c6")]:
        net.connect(a, pa, b, None)
    net.connect("r1", 5, "r2", 5, "control")
    net.connect("r1", 6, "r2", 6, "control-backup")
    net.connect("r1", 4, "r2", 4, "fabric")
    net.group_add("fab1", ["s1", "s2"])
    net.group_add("fab2", ["s3", "s4"])
    net.group_add("rfab1", ["r1", "r2"])
    net.clusters["rfab1"] = "none"
    return net, ("[green]HA demo loaded[/]: r1+r2 form cluster rfab1 between fab1 and fab2.\n"
                 "  try [cyan]ping c1 c6[/], then [cyan]fail r1:5[/], [cyan]fail r1:6[/] (split-brain), "
                 "[cyan]cluster rfab1 policy=witness[/], [cyan]fail r1[/], [cyan]fail r1:2[/], [cyan]restore all[/]")


def loop() -> tuple[Network, str]:
    """Three switches cabled in a triangle with spanning tree switched off."""
    net = Network()
    net.add("r", 2)
    for _ in range(3):
        net.add("s", 6)
    for _ in range(3):
        net.add("c", 1)
    for a, b in [("r1", "s1"), ("s1", "s2"), ("s2", "s3"), ("s3", "s1"),
                 ("s1", "c1"), ("s2", "c2"), ("s3", "c3")]:
        net.connect(a, None, b, None)
    net.group_add("ring", ["s1", "s2", "s3"])
    net.stp_off = {"s1", "s2", "s3"}
    return net, ("[green]loop demo loaded[/]: s1–s2–s3 form a triangle and STP is off.\n"
                 "  try [cyan]broadcast c1[/] (or [cyan]ping c1 c2[/]) to set off a storm, then "
                 "[cyan]stp on[/], [cyan]stp[/], [cyan]fail s1:2[/], [cyan]ping c1 c2[/]")


def core() -> tuple[Network, str]:
    """Three sites joined only through a redundant, routed core (an active-passive HA pair).

    Every site↔core cable is its own little network, so no two sites share one:
    traffic between sites has to be routed by a core router."""
    net = Network()
    for _ in range(2):
        net.add("r", 8)                         # r1, r2: the core
    for _ in range(6):
        net.add("r", 6)                         # r3–r8: two routers per site
    for _ in range(3):
        net.add("s", 6)                         # s1–s3: one LAN switch per site
    for _ in range(6):
        net.add("c", 1)                         # c1–c6: two computers per site
    net.group_add("core", ["r1", "r2"])
    net.clusters["core"] = "witness"
    net.cluster_modes["core"] = "active-passive"
    net.connect("r1", 7, "r2", 7, "control")
    net.connect("r1", 8, "r2", 8, "control-backup")
    for i in range(3):
        ra, rb, sw = f"r{3 + 2 * i}", f"r{4 + 2 * i}", f"s{1 + i}"
        ca, cb = f"c{1 + 2 * i}", f"c{2 + 2 * i}"
        net.group_add(f"rtr{i + 1}", [ra, rb])
        net.group_add(f"lan{i + 1}", [sw, ca, cb])
        net.group_add(f"site{i + 1}", [f"rtr{i + 1}", f"lan{i + 1}"])
        for d in (ra, rb, ca, cb):
            net.connect(d, None, sw, None)
    for r in ("r3", "r4"):                      # site 1's routers to both core routers…
        for c in ("r1", "r2"):
            net.connect(r, None, c, None)
    net.copy_cabling("rtr1", "rtr2", [("core", "core")])   # …and the same pattern for sites 2 and 3
    net.copy_cabling("rtr1", "rtr3", [("core", "core")])
    return net, ("[green]routed core demo loaded[/]: three sites, joined only through core (r1 active, "
                 "r2 standby).\n  try [cyan]ping c1 c3[/] (via the core), [cyan]ping c1 c2[/] (stays local), "
                 "[cyan]fail r1[/], [cyan]fail r3:3[/], [cyan]fail r1:7[/], [cyan]traffic on 6[/]")


DEMOS: dict[str, Callable[[], tuple[Network, str]]] = {"basic": basic, "ha": ha, "loop": loop, "core": core}
