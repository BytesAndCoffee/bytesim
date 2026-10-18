import pytest

from bytesim import demos
from bytesim.model.traffic import TrafficGenerator, drop_reason
from bytesim.scripting import ScriptRunner


def running(commands, steps=30, dt=0.5):
    r = ScriptRunner()
    r.run(commands)
    r.net.reconcile()
    for _ in range(steps):
        r.traffic_step(dt)
    return r


def test_pacing_follows_the_rate():
    gen = TrafficGenerator(rate=4)
    assert sum(gen.due(0.1) for _ in range(50)) == 20      # 5 seconds at 4/s, no bursts


def test_same_seed_same_flows_and_hosts_are_respected():
    net, _ = demos.basic()
    one, two, other = TrafficGenerator(seed=1), TrafficGenerator(seed=1), TrafficGenerator(seed=9)
    flows = [one.pick(net) for _ in range(10)]
    assert [two.pick(net) for _ in range(10)] == flows
    assert [other.pick(net) for _ in range(10)] != flows
    g = TrafficGenerator(seed=2, hosts={"c1", "c2"}, mix={"unicast": 1})
    assert all(f[1] in {"c1", "c2"} and f[2] in {"c1", "c2"} and f[0] == "unicast"
               for f in (g.pick(net) for _ in range(20)))


def test_no_gateway_means_no_internet_traffic():
    net, _ = demos.loop()                                   # no gateway in the loop demo
    g = TrafficGenerator(seed=3, mix={"internet": 1, "unicast": 1})
    assert all(g.pick(net)[0] == "unicast" for _ in range(20))


def test_traffic_flows_and_is_counted():
    r = running("demo basic\ntraffic on 10 seed=7")
    st = r.traffic.stats
    assert st.sent > 50 and st.delivered == st.sent        # the stub screen delivers at once
    assert st.cables and st.forwarded["r1"] > 0
    assert st.dropped["unreachable"] > 0                   # the ad-hoc pair c9/c10 has no gateway
    assert r.net.mac_tables                                # switches learned from the traffic


def test_failures_show_up_as_drops():
    r = running("demo ha\nfail r1\nfail r2\ntraffic on 10 seed=4 mix=unicast")
    assert r.traffic.stats.dropped and r.traffic.stats.delivered < r.traffic.stats.sent + sum(
        r.traffic.stats.dropped.values())


def test_random_traffic_sets_off_a_storm_in_a_live_loop():
    r = running("demo loop\ntraffic on 10 seed=1", steps=10)
    assert r.net.storming == {"s1", "s2", "s3"} and r.traffic.stats.dropped["storm"] > 0


def test_heat_cools_down():
    net, _ = demos.loop()
    g = TrafficGenerator()
    g.carry(net, [("c1", -1), ("s1", net.link_index("c1", 1))])
    link = net.links[net.link_index("c1", 1)]
    hot = g.level(link)
    g.cool(10)
    assert hot > 0 and g.level(link) == 0


@pytest.mark.parametrize("message, reason", [
    ("c9 has no address (try: dhcp c9)", "no address"),
    ("x — broadcast storm on s1 is saturating the links", "storm"),
    ("r1 is down", "device down"),
    ("network 3 isn't local to c9 and c9 has no gateway", "unreachable"),
])
def test_drop_reasons(message, reason):
    assert drop_reason(message) == reason
