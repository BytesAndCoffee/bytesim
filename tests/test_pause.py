"""Pause freezes playback and batches packet commands for a common start."""
import asyncio

import pytest

from bytesim import NetError, demos
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp
from bytesim.tui.canvas import PACKET_FRAMES


def runner():
    net, _ = demos.basic()
    net.reconcile()
    net.auto_layout(100)
    return ScriptRunner(net)


def test_paused_actions_start_on_the_same_animation_tick_and_finish():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(170, 60)) as pilot:
            app.run_command("demo ha")
            app.run_command("pause")
            before = app.net.to_json()
            for command in ("ping c1 c2", "ping c3 c4", "mping c1 c4 c6", "unicast c2 c3"):
                app.run_command(command)
            assert len(app.pending_actions) == 4
            assert not app.canvas.packets and app.net.to_json() == before
            await pilot.pause()
            assert not app.canvas.packets
            assert "paused" in app.query_one("#info").render().plain
            assert "4 queued" in app.query_one("#info").render().plain
            app.run_command("resume")
            assert not app.paused and not app.pending_actions
            assert len(app.canvas.packets) == 5
            assert all(p.pos == 0 for p in app.canvas.packets)
            app.canvas._tick()
            assert len({p.pos for p in app.canvas.packets}) == 1
            for _ in range(3 * PACKET_FRAMES):
                if not app.canvas.busy:
                    break
                app.canvas._tick()
            assert not app.canvas.busy and not app.canvas.hot_links
            text = " ".join(" ".join(line.text for line in app.query_one("#log").lines).split())
            assert "reply from c2" in text and "reply from c4" in text
            assert "multicast ping: 2/2 replies, 0 failed" in text
            assert "delivered to c3" in text

    asyncio.run(go())


def test_pause_freezes_in_flight_packets_flash_and_frame_clock():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(170, 60)) as pilot:
            app.run_command("demo ha")
            app.run_command("ping c1 c6")
            app.canvas.flash_device("c1", "red")
            app.canvas._tick()
            app.run_command("pause")
            state = ([p.pos for p in app.canvas.packets], app.canvas.frame, app.canvas.flash["c1"][1])
            for _ in range(10):
                app.canvas._tick()
            await pilot.pause(0.1)
            assert ([p.pos for p in app.canvas.packets], app.canvas.frame, app.canvas.flash["c1"][1]) == state
            app.run_command("play")
            app.canvas._tick()
            assert app.canvas.packets[0].pos > state[0][0]
            app.canvas.reset()

    asyncio.run(go())


def test_background_traffic_stops_without_accumulating_a_catchup_burst():
    r = runner()
    r.run("traffic on 4 seed=1 mix=unicast")
    r.run("pause")
    before = r.traffic.stats.sent
    assert r.traffic_step(100) == 0
    assert r.traffic.stats.sent == before
    r.run("resume")
    assert r.traffic_step(0.25) == 1


def test_queue_is_inspectable_and_can_be_cleared():
    r = runner()
    r.run("pause\nping c1 c2\nmping c1 all\nqueue")
    assert len(r.pending_actions) == 2
    assert "1. ping c1 c2" in "\n".join(r.log)
    assert "2. mping c1 all" in "\n".join(r.log)
    assert "reply from" not in "\n".join(r.log)
    r.run("queue clear\nresume")
    assert not r.pending_actions and not r.paused
    assert "reply from" not in "\n".join(r.log)


def test_queued_actions_use_current_topology_and_one_failure_does_not_stop_the_batch():
    r = runner()
    r.run("pause\nping c1 c6\nping c1 c2\nfail c6\nresume")
    text = "\n".join(r.log)
    assert "✗ queued ping c1 c6" in text
    assert "reply from c2" in text and "reply from c6" not in text
    assert "1 failed" in text and not r.pending_actions


@pytest.mark.parametrize("command", ("ping c1 c2", "mping c1 all", "mcast ping c1 c2", "send c1 c2",
                                    "mcast c1 c2", "broadcast c1", "dhcp c1"))
def test_all_packet_command_aliases_queue_without_running(command):
    r = runner()
    r.run("pause\n" + command)
    assert len(r.pending_actions) == 1
    assert r.pending_actions[0] == r.tokens(command)
    assert "reply from" not in "\n".join(r.log)


def test_replacing_the_network_discards_queued_actions_but_preserves_pause():
    r = runner()
    r.run("pause\nping c1 c2\nclear")
    assert r.paused and not r.pending_actions
    r.run("resume")
    assert "reply from" not in "\n".join(r.log)


@pytest.mark.parametrize("command", ("pause now", "resume now", "queue nope"))
def test_invalid_playback_controls_do_not_change_the_queue(command):
    r = runner()
    r.run("pause\nping c1 c2")
    with pytest.raises(NetError, match="usage:"):
        r.run(command)
    assert r.paused and len(r.pending_actions) == 1
