"""Trace and detail are live session settings shared by console and CLI hosts."""
import asyncio

import pytest
from rich.text import Text

from bytesim import NetError, demos
from bytesim.cli import build_parser
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp


def runner():
    net, _ = demos.basic()
    net.reconcile()
    net.auto_layout(100)
    return ScriptRunner(net)


@pytest.mark.parametrize("command,field,expected", (
    ("trace on", "trace_enabled", True),
    ("trace off", "trace_enabled", False),
    ("detail on", "detail_enabled", True),
    ("detail off", "detail_enabled", False),
    ("detail auto", "detail_enabled", None),
))
def test_global_settings_and_status_are_independent_of_the_network(command, field, expected):
    r = runner()
    before = r.net.to_json()
    r.run(command)
    assert getattr(r, field) is expected
    r.run(command.split()[0])
    assert Text.from_markup(r.log[-1]).plain == command.replace(" ", ": ")
    assert r.net.to_json() == before


@pytest.mark.parametrize("command", ("trace auto", "trace maybe", "trace on off", "detail maybe", "detail on off"))
def test_invalid_global_settings_preserve_previous_values(command):
    r = runner()
    r.run("trace on\ndetail off")
    before = len(r.log)
    with pytest.raises(NetError, match="usage:"):
        r.run(command)
    assert r.trace_enabled and r.detail_enabled is False and len(r.log) == before


@pytest.mark.parametrize("command", ("mping all all trace=on", "ping c1 c2 detail=off",
                                     "mcast c1 c2 detail=on", "detail=on", "trace on detail=off"))
def test_per_command_options_are_rejected_even_while_paused(command):
    r = runner()
    r.run("pause")
    with pytest.raises(NetError, match="global setting|missing command"):
        r.run(command)
    assert r.pending_actions == []
    assert not r.trace_enabled and r.detail_enabled is None


def test_settings_survive_network_replacement_and_are_not_shared_between_runners():
    r = runner()
    r.run("trace on\ndetail off\nclear\ndemo ha")
    assert r.trace_enabled and r.detail_enabled is False
    assert "trace" not in r.net.to_json() and "detail" not in r.net.to_json()
    another = runner()
    assert not another.trace_enabled and another.detail_enabled is None


@pytest.mark.parametrize("initial,final", (("on", "off"), ("off", "on")))
def test_switching_global_flags_affects_subsequent_in_flight_output(monkeypatch, initial, final):
    r = runner()
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append(on_done))
    r.run(f"trace {initial}\ndetail {initial}\nmping c1 c2")
    assert any("TRACE REQUEST" in m for m in r.log) == (initial == "on")
    r.log.clear()
    r.run(f"trace {final}\ndetail {final}")
    while packets:
        packets.pop(0)()
    assert any("TRACE REPLY" in m for m in r.log) == (final == "on")
    assert any("ECHO REPLY" in m for m in r.log) == (final == "on")
    assert any("reply from c2" in m for m in r.log) == (final == "on")
    assert any("multicast ping: 1/1 replies, 0 failed" in m for m in r.log)


def test_paused_actions_use_the_global_flags_at_resume():
    r = runner()
    r.run("pause\nmping c1 c2\ntrace on\ndetail off")
    assert r.pending_actions == [["mping", "c1", "c2"]]
    r.run("resume")
    assert sum("TRACE REQUEST" in m for m in r.log) == 1
    assert sum("TRACE REPLY" in m for m in r.log) == 1
    assert not any("ECHO REPLY" in m for m in r.log)


def test_trace_and_detail_also_apply_to_ping_unicast_and_one_way_multicast():
    r = runner()
    r.run("trace on\ndetail off\nping c1 c2\nunicast c1 c2\nmcast c1 c2")
    assert sum("TRACE REQUEST" in m for m in r.log) == 2
    assert sum("TRACE REPLY" in m for m in r.log) == 1
    assert sum("TRACE UNICAST" in m for m in r.log) == 1
    assert not any("ARP " in m for m in r.log)
    r.log.clear()
    r.run("trace off\ndetail on\nping c1 c2")
    assert any("ARP " in m for m in r.log) and not any("TRACE " in m for m in r.log)


def test_global_trace_handles_internet_routes_without_a_device_destination():
    r = runner()
    r.run("trace on\ndetail off\nping c1 internet\nunicast c1 internet")
    assert sum("TRACE REQUEST" in m for m in r.log) == 1
    assert sum("TRACE REPLY" in m for m in r.log) == 1
    assert sum("TRACE UNICAST" in m for m in r.log) == 1
    assert any("→ internet" in m for m in r.log if "TRACE" in m)


@pytest.mark.parametrize("args,trace,detail", (([], False, None), (["--trace", "--detail"], True, True),
                                               (["--trace", "--no-detail"], True, False),
                                               (["--no-trace"], False, None)))
def test_cli_global_flags(args, trace, detail):
    parsed = build_parser().parse_args(args)
    assert parsed.trace is trace and parsed.detail is detail


def test_real_ui_settings_are_visible_and_survive_undo_without_cancelling_packets():
    async def go():
        app = ByteSimApp(trace=True, detail=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("demo ha")
            steps = len(app.undo_stack)
            app.run_command("mping c1 c2")
            packets = list(app.canvas.packets)
            app.run_command("trace off")
            app.run_command("detail on")
            assert app.canvas.packets == packets and app.canvas.busy
            assert len(app.undo_stack) == steps
            info = app.query_one("#info").render().plain
            assert "trace: off" in info and "detail: on" in info
            app.run_command("undo")
            assert not app.trace_enabled and app.detail_enabled is True
            app.run_command("redo")
            assert not app.trace_enabled and app.detail_enabled is True

    asyncio.run(go())
