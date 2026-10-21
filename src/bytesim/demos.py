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


def mlag() -> tuple[Network, str]:
    """A seven-tier data centre: edge gateway → clustered routers → spine → two pods of
    aggregation → leaf → rack switches → computers.

    Spine, aggregation and leaf are MLAG pairs, and each rack switch is dual-homed to
    its leaf pair. Every span between tiers is a full mesh bundled into one link
    (back-to-back MLAG, like double-sided vPC), so STP blocks nothing, yet any one
    router, spine/aggregation/leaf switch or uplink cable can fail without cutting a
    rack off. Each rack switch, like a real single top-of-rack switch, is still its
    own rack's single point of failure (computers can't be bundled)."""
    net = Network()
    net.add("g", 2)                             # g1: the edge
    for _ in range(2):
        net.add("r", 6)                         # r1, r2: the routed core, one cluster
    for _ in range(2):
        net.add("s", 10)                        # s1, s2: spine
    for _ in range(4):
        net.add("s", 10)                        # s3–s6: aggregation, a pair per pod
    for _ in range(8):
        net.add("s", 8)                         # s7–s14: leaves, two pairs per pod
    for _ in range(4):
        net.add("s", 6)                         # s15–s18: a rack switch under each leaf pair
    for _ in range(8):
        net.add("c", 1)                         # c1–c8: two computers per rack

    def pair(name: str, a: str, b: str) -> None:
        net.group_add(name, [a, b])
        net.connect(a, None, b, None)           # the peer link
        net.mlag_domains.add(name)

    def mesh(upper: tuple[str, str], lower: tuple[str, ...]) -> None:
        for d in lower:
            for u in upper:
                net.connect(u, None, d, None)

    net.group_add("core", ["r1", "r2"])
    net.clusters["core"] = "witness"
    net.connect("r1", 5, "r2", 5, "control")
    net.connect("r1", 6, "r2", 6, "control-backup")
    for r in ("r1", "r2"):
        net.connect("g1", None, r, None)
    pair("spine", "s1", "s2")
    mesh(("s1", "s2"), ("r1", "r2"))
    net.lags |= {frozenset(("r1", "spine")), frozenset(("r2", "spine"))}
    for pod in (1, 2):
        agg = (f"s{1 + 2 * pod}", f"s{2 + 2 * pod}")       # s3 s4 · s5 s6
        pair(f"agg{pod}", *agg)
        mesh(("s1", "s2"), agg)
        net.lags.add(frozenset(("spine", f"agg{pod}")))
        members = [f"agg{pod}"]
        for side in "ab":
            k = 4 * pod + 2 * (side == "b")              # leaves s7 s8 · s9 s10 · s11 s12 · s13 s14
            leaf = (f"s{k + 3}", f"s{k + 4}")
            rack = f"s{14 + 2 * pod - (side == 'a')}"    # s15 s16 · s17 s18
            n = 4 * pod - 3 + 2 * (side == "b")          # c1 c2 · c3 c4 · c5 c6 · c7 c8
            hosts = [f"c{n}", f"c{n + 1}"]
            pair(f"leaf{pod}{side}", *leaf)
            mesh(agg, leaf)
            mesh(leaf, (rack,))
            for c in hosts:
                net.connect(c, None, rack, None)
            net.lags |= {frozenset((f"agg{pod}", f"leaf{pod}{side}")),
                         frozenset((f"leaf{pod}{side}", rack))}
            net.group_add(f"hosts{pod}{side}", hosts)    # a subgroup, so they get their own row
            net.group_add(f"rack{pod}{side}", [rack, f"hosts{pod}{side}"])
            members += [f"leaf{pod}{side}", f"rack{pod}{side}"]
        net.group_add(f"pod{pod}", members)
    return net, ("[green]MLAG demo loaded[/]: g1 → core (r1+r2) → spine → agg → leaf → rack, "
                 "each span one bundle.\n  try [cyan]mlags[/], [cyan]lags[/], "
                 "[cyan]ping c1 c8[/], [cyan]fail s1[/], [cyan]fail s7[/], [cyan]fail r1[/] (all survivable), "
                 "[cyan]fail s15[/] (a single rack switch cuts off its rack), [cyan]mping all all[/]")


def internet() -> tuple[Network, str]:
    """Three sites behind NAT gateways, joined only through the public internet.

    g1 and g2 are the ISP edge, peering over an exchange switch s1: the group ``isp``,
    declared the internet. c7 is a public server on g2's network, behind no NAT. Each
    site's gateway (g3–g5) has a WAN port into the ISP and runs NAT, so sites reach the
    internet and the server, but not each other's insides."""
    net = Network()
    for _ in range(2):
        net.add("g", 4)                         # g1, g2: the ISP
    for _ in range(3):
        net.add("g", 2)                         # g3–g5: one NAT gateway per site
    for _ in range(4):
        net.add("s", 4)                         # s1: the internet exchange; s2–s4: one LAN switch per site
    for _ in range(7):
        net.add("c", 1)                         # c1–c6: two computers per site; c7: a public server
    net.connect("g1", None, "s1", None)
    net.connect("g2", None, "s1", None)
    net.connect("g2", None, "c7", None)
    net.group_add("isp", ["g1", "g2", "s1"])
    net.group_add("hosting", ["c7"])            # a server hosted on the ISP's network, outside every NAT
    net.internet = "isp"
    for i, up in enumerate(("g1", "g1", "g2")):
        gw, sw = f"g{3 + i}", f"s{2 + i}"
        hosts = [f"c{1 + 2 * i}", f"c{2 + 2 * i}"]
        net.connect(up, None, gw, None)          # the site's WAN port: port 1
        net.connect(gw, None, sw, None)
        for c in hosts:
            net.connect(c, None, sw, None)
        net.group_add(f"pcs{1 + i}", hosts)      # a subgroup, so they get their own row
        net.group_add(f"lan{1 + i}", [sw, f"pcs{1 + i}"])
        net.group_add(f"site{1 + i}", [gw, f"lan{1 + i}"])
    return net, ("[green]internet demo loaded[/]: sites 1–3 sit behind NAT gateways g3–g5; the ISP "
                 "(g1, g2) and a public server c7 are the internet.\n  try [cyan]ping c1 c7[/] (out through NAT), "
                 "[cyan]ping c1 c3[/] (dropped: site 2 is behind NAT), [cyan]internet[/], "
                 "[cyan]nat g4 off[/], [cyan]ping c1 c3[/], [cyan]mping all all[/]")


def overlay() -> tuple[Network, str]:
    """The internet demo plus an overlay, ol: one computer per site and the public server c7.

    c7 also runs the coordination server and c8 (also public) a relay. Sites 2
    and 3 have hard NAT, so c3 and c5 can't hole-punch to each other and relay via c8;
    every other pair goes direct."""
    net, _ = internet()
    c8 = net.add("c", 1).id                     # c8: a second public box, for the relay
    net.connect("g2", None, c8, None)
    net.group_add("hosting", [c8])
    net.nat_hard |= {"g4", "g5"}
    net.relays = [c8]
    net.new_overlay("ol", 100)["coord"] = "c7"
    for d in ("c1", "c3", "c5", "c7"):
        net.join_overlay("ol", d)
    return net, ("[green]overlay demo loaded[/]: c1, c3, c5 (behind NAT; sites 2 and 3 hard NAT) and the public "
                 "server c7 are in overlay ol; c7 coordinates, c8 relays.\n  try [cyan]ping c1 c3@ol[/] (direct), "
                 "[cyan]ping c3 c5@ol[/] (relayed via c8), [cyan]overlay ol[/], [cyan]fail c8[/], "
                 "[cyan]fail c7[/] then [cyan]ping c1 c3@ol[/] (still works) and [cyan]overlay ol c2[/] (can't join)\n"
                 "  then [cyan]overlay ol advertise c3[/] + [cyan]ping c1 c4[/] (subnet router), [cyan]overlay ol exit=c7[/] + "
                 "[cyan]ping c1 internet[/] (exit node), [cyan]acl ol allow c1 c3[/] (rules on: the rest is denied)")


DEMOS: dict[str, Callable[[], tuple[Network, str]]] = {"basic": basic, "ha": ha, "loop": loop, "core": core,
                                                          "mlag": mlag,
                                                          "internet": internet,
                                                          "overlay": overlay}
