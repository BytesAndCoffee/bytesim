"""Large multicast batches yield, stay simultaneous, and release every packet."""
import asyncio
from types import SimpleNamespace

import pytest

from bytesim import NetError, Network
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp, canvas as canvas_module
from bytesim.tui.canvas import TRAIL, WORK_SECONDS


def network(count=12):
    net = Network()
    net.add("s", count)
    for _ in range(count):
        dev = net.add("c")
        net.connect("s1", None, dev.id, None)
    net.reconcile()
    net.auto_layout(120)
    return net


def one_step_budget(monkeypatch):
    clock = [0.0]

    def now():
        clock[0] += WORK_SECONDS * 0.75
        return clock[0]

    # Replace only the canvas's clock, not Python's time module or asyncio's clock.
    monkeypatch.setattr(canvas_module, "time", SimpleNamespace(perf_counter=now))


def test_bulk_pause_start_callback_backlog_and_complete_accounting(monkeypatch):
    one_step_budget(monkeypatch)

    async def go():
        app = ByteSimApp(network(), auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            messages = []
            monkeypatch.setattr(app, "log_msg", messages.append)
            c = app.canvas
            app.run_command("mping all all")
            assert c.preparing and c.busy and not c.packets
            c._tick()
            app.run_command("pause")
            before = len(c._preparing), len(c.packets)
            for _ in range(10):
                c._tick()
                c._work()
            assert (len(c._preparing), len(c.packets)) == before
            app.run_command("resume")
            for _ in range(400):
                if not c.preparing:
                    break
                c._tick()
            assert not c.preparing
            assert len(c.packets) == 132 and all(p.pos == 0 for p in c.packets)

            # Force a simultaneous arrival burst, leaving most callbacks queued.
            for p in c.packets:
                p.pos = len(p.cells) + TRAIL
            c._tick()
            assert c._callbacks and c.busy
            app.run_command("pause")
            before = len(c._callbacks), len(c.packets)
            c._work()
            c._tick()
            assert (len(c._callbacks), len(c.packets)) == before
            app.run_command("resume")
            for _ in range(600):
                if not c.busy:
                    break
                c._tick()
            assert not c.busy and not c.hot_links
            assert sum("11/11 replies, 0 failed" in m for m in messages) == 12
            assert not any("ECHO REPLY" in m for m in messages)
            assert app.net._memo is None and app.net._route_memo is None

    asyncio.run(go())


def test_bulk_preparation_really_yields_to_the_event_loop(monkeypatch):
    one_step_budget(monkeypatch)

    async def go():
        app = ByteSimApp(network(), auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("mping all all")
            assert app.canvas.preparing
            # Independent tasks can run while routes are still being prepared.
            await asyncio.sleep(0.01)
            app.run_command("pause")
            assert app.canvas.preparing and app.paused
            assert "preparing packet batch" in app.query_one("#info").render().plain
            app.canvas.reset()
            assert not app.canvas.busy

    asyncio.run(go())


def test_queued_bulk_and_small_actions_share_the_same_start(monkeypatch):
    one_step_budget(monkeypatch)

    async def go():
        app = ByteSimApp(network(), auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("pause")
            app.run_command("mping all all")
            app.run_command("ping c1 c2")
            app.run_command("resume")
            c = app.canvas
            assert c.preparing and len(c.packets) == 1
            for _ in range(400):
                if not c.preparing:
                    break
                c._tick()
            assert not c.preparing and len(c.packets) == 133
            assert all(p.pos == 0 for p in c.packets)
            # An unchanged refresh must preserve the batch and its highlights.
            hot = c.hot_links.copy()
            app.changed()
            assert c.busy and c.hot_links == hot
            c._tick()
            assert len({p.pos for p in c.packets}) == 1
            c.reset()

    asyncio.run(go())


@pytest.mark.parametrize("command", ("fail c1", "layout grid", "mode graph", "clear"))
def test_topology_changes_cancel_bulk_preparation_without_stale_callbacks(monkeypatch, command):
    one_step_budget(monkeypatch)

    async def go():
        app = ByteSimApp(network(), auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("mping all all")
            c = app.canvas
            # Prepare one source, with packets and heat, while the rest waits.
            for _ in range(30):
                c._tick()
            assert c.preparing and c.packets and c.hot_links
            app.run_command(command)
            assert not c.busy and not c.hot_links and not c.flash
            c._work()  # an already scheduled callback must be harmless
            assert not c.busy

    asyncio.run(go())


def test_a_storm_discovered_during_preparation_cancels_safely(monkeypatch):
    one_step_budget(monkeypatch)
    net = network()
    net.resize("s1", 14)
    net.add("s", 3)
    net.add("s", 3)
    for a, b in (("s1", "s2"), ("s2", "s3"), ("s3", "s1")):
        net.connect(a, None, b, None)
    net.stp_off = {"s1", "s2", "s3"}
    net.reconcile()

    async def go():
        app = ByteSimApp(net, auto=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("mping all all")
            assert app.canvas.preparing
            app.canvas._tick()
            assert app.net.storming
            assert not app.canvas.preparing and not app.canvas.busy
            assert app.net._memo is None and app.net._route_memo is None

    asyncio.run(go())


def test_bulk_summaries_count_failures():
    r = ScriptRunner(network())
    r.net.devices["c12"].up = False
    r.run("mping all all")
    summaries = [m for m in r.log if "multicast ping:" in m]
    assert len(summaries) == 12
    assert sum("10/11 replies, 1 failed" in m for m in summaries) == 11
    assert sum("0/11 replies, 11 failed" in m for m in summaries) == 1


def test_routing_indexes_and_query_results_do_not_survive_changes():
    net = network(3)
    assert net.route("c1", ("c2", 1))
    link = net.link_on("c2", 1)
    with net.remembering():
        assert ("c2", 1) in net.segment("c1", 1)
        link.up = False
        assert ("c2", 1) not in net.segment("c1", 1)
        assert net.l2path(("c1", 1), ("c2", 1)) is None
    with pytest.raises(NetError):
        net.route("c1", ("c2", 1))
    assert net._memo is None and net._route_memo is None
    link.up = True
    assert net.route("c1", ("c2", 1))
    net.disconnect("c1", None, None)
    net.resize("c3", 2)
    net.connect("s1", 1, "c3", 2)  # reuses the first cable's old switch port
    with net.remembering():
        assert net.link_index("c1", 1) is None
        assert net.link_on("s1", 1).b == "c3"
        assert net.adjacent_links("c1") == []
    assert net._memo is None
