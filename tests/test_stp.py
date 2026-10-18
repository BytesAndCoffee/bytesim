from bytesim import demos
from bytesim.model import Network


def _loop(stp: bool = False) -> Network:
    net, _ = demos.loop()
    if stp:
        net.stp_off.clear()
    net.reconcile()
    return net


def test_a_live_loop_storms_and_a_tree_does_not(basic):
    net = _loop()
    assert net.loop_links()                       # the triangle, STP off
    fl = net.flood(("c1", 1))
    assert fl.storm and fl.storm_switches == {"s1", "s2", "s3"}
    assert set(fl.flaps) == {"s1", "s2", "s3"}    # the sender's MAC arrives on several ports
    # a loop-free network delivers exactly one copy to every interface it reaches
    once = basic.flood(("c1", 1))
    assert not once.storm and set(once.delivered.values()) == {1}


def test_stp_blocks_one_port_per_redundant_cable():
    net = _loop(stp=True)
    assert net.stp_blocked == {("s3", 1)}
    assert not net.loop_links() and not net.flood(("c1", 1)).storm
    assert net.stp_roots["s2"] == "s1" and net.port_role("s3", 1) == "blocking"


def test_one_stp_switch_can_break_a_loop_of_dumb_switches():
    net = _loop()
    net.stp_off.discard("s2")                     # STP frames pass through s1 and s3
    net.reconcile()
    assert all(sw == "s2" for sw, _ in net.stp_blocked)
    assert not net.loop_links()


def test_parallel_cables_are_a_loop_too():
    net = Network()
    net.add("s"); net.add("s"); net.add("c")
    net.connect("s1", None, "s2", None)
    net.connect("s1", None, "s2", None)
    net.connect("c1", None, "s1", None)
    net.reconcile()
    assert len(net.stp_blocked) == 1
    net.stp_off = {"s1", "s2"}
    net.reconcile()
    assert net.flood(("c1", 1)).storm


def test_a_storm_saturates_until_the_loop_is_broken():
    net = _loop()
    net.start_storm(net.flood(("c1", 1)))
    assert net.storming == {"s1", "s2", "s3"}
    assert not any(net.usable(l) for l in net.links if "s1" in (l.a, l.b))
    assert net.storm_near("c2") == {"s1", "s2", "s3"}
    net.stp_off.clear()
    notes = net.stp_update()
    assert any("storm over" in n for n in notes) and not net.storming


def test_failing_a_forwarding_link_unblocks_the_backup():
    net = _loop(stp=True)
    net.links[net.link_index("s1", 2)].up = False
    notes = net.stp_update()
    assert not net.stp_blocked and "STP s3:p1 blocking → forwarding" in notes


def test_priority_picks_the_root_and_settings_survive_save_and_renumber():
    net = _loop(stp=True)
    net.stp_priority["s3"] = 4096
    net.stp_update()
    assert net.stp_roots["s1"] == "s3"
    again = Network.from_json(net.to_json())
    assert again.stp_priority == {"s3": 4096} and again.stp_off == set()
    net.stp_off = {"s3"}
    net.renumber("dfs", "c3")                     # s3 is reached first now
    assert net.stp_off == {"s1"} and net.stp_priority == {"s1": 4096}
