"""Optional multicast traces describe real request and arrival-time reply paths."""
import asyncio
import re

import pytest
from rich.text import Text

from bytesim import NetError, Network
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp


def network():
    net = Network()
    for _ in range(2):
        net.add("r", 4)
        net.add("s", 4)
        net.add("c")
    for a, pa, b, pb, role in (
        ("c1", 1, "s1", 1, "data"),
        ("s1", 2, "r1", 1, "data"),
        ("r1", 2, "r2", 1, "fabric"),
        ("r2", 2, "s2", 1, "data"),
        ("s2", 2, "c2", 1, "data"),
    ):
        net.connect(a, pa, b, pb, role)
    net.reconcile()
    net.auto_layout(100)
    return net


def traces(r):
    return [Text.from_markup(m).plain for m in r.log if "TRACE " in m]


@pytest.mark.parametrize("command", ("mping c1 c2", "mcast ping c1 c2", "multicast-ping c1 c2"))
def test_full_traces_include_all_devices_ports_cable_roles_and_both_directions(command):
    r = ScriptRunner(network())
    r.run("trace on\ndetail off\n" + command)
    request, reply = traces(r)
    assert request.startswith("TRACE REQUEST c1 → c2 · endpoints c1:nic1")
    assert "→ c2:nic1 (" in request
    assert request.endswith(
        "c1:nic1 -data#1-> s1:p1 | s1:p2 -data#2-> r1:p1 | "
        "r1:p2 -fabric#3-> r2:p1 | r2:p2 -data#4-> s2:p1 | s2:p2 -data#5-> c2:nic1")
    assert reply.startswith("TRACE REPLY c2 → c1 · endpoints c2:nic1")
    assert reply.endswith(
        "c2:nic1 -data#5-> s2:p2 | s2:p1 -data#4-> r2:p2 | "
        "r2:p1 -fabric#3-> r1:p2 | r1:p1 -data#2-> s1:p2 | s1:p1 -data#1-> c1:nic1")
    assert any("multicast ping: 1/1 replies, 0 failed" in m for m in r.log)


@pytest.mark.parametrize("option", ("", "trace off", "detail on"))
def test_tracing_is_off_by_default_and_independent_of_detail(option):
    r = ScriptRunner(network())
    r.run(f"{option}\nmping c1 c2")
    assert traces(r) == []


def test_reply_trace_uses_a_fresh_route_after_arrival_not_the_reversed_request(monkeypatch):
    net = network()
    net.connect("r1", 3, "r2", 3, "fabric")
    net.reconcile()
    r = ScriptRunner(net)
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append(on_done))
    r.run("trace on\ndetail off\nmping c1 c2")
    request, = traces(r)
    selected = int(re.search(r"fabric#(\d+)", request)[1])
    net.links[selected - 1].up = False
    packets.pop(0)()
    assert len(traces(r)) == 2
    reply = traces(r)[1]
    assert "TRACE REPLY c2 → c1" in reply
    assert f"fabric#{selected}" not in reply and "fabric#" in reply
    packets.pop(0)()
    assert any("multicast ping: 1/1 replies, 0 failed" in m for m in r.log)


def test_failures_trace_the_reason_and_do_not_invent_a_return_path(monkeypatch):
    r = ScriptRunner(network())
    packets = []
    monkeypatch.setattr(r.canvas, "send_packet", lambda cells, color, on_done=None: packets.append(on_done))
    r.run("trace on\ndetail off\nmping c1 c2")
    assert len(traces(r)) == 1
    r.net.iface("c2", 1).gw = None
    packets.pop(0)()
    assert traces(r)[1].startswith("TRACE REPLY FAILED c2 → c1:")
    assert "has no gateway" in traces(r)[1] and not packets
    assert any("multicast ping: 0/1 replies, 1 failed" in m for m in r.log)
    r.log.clear()
    r.net.devices["c1"].up = False
    r.run("trace on\ndetail off\nmping c1 c2")
    assert len(traces(r)) == 1 and traces(r)[0].startswith("TRACE REQUEST FAILED c1 → c2:")


def test_graph_traces_show_physical_paths_without_address_endpoints():
    r = ScriptRunner(network())
    r.run("trace on\ndetail off\nmode graph\nmping s1 c2")
    request, reply = traces(r)
    assert "endpoints" not in request + reply
    assert "s1:p2 -data#2-> r1:p1" in request
    assert "c2:nic1 -data#5-> s2:p2" in reply


def test_trace_survives_bulk_summary_dispatch_and_whole_network_expansion(monkeypatch):
    r = ScriptRunner(network())
    monkeypatch.setattr(r, "BULK_PAIRS", 1)
    r.run("trace on\nmping all all *")
    assert len(traces(r)) == 24  # four addressed devices, twelve ordered pairs
    assert sum("multicast ping: 3/3 replies, 0 failed" in m for m in r.log) == 4
    assert not any("ECHO REPLY" in m for m in r.log)  # summaries remain independent


def test_invalid_trace_values_do_not_start_or_mutate_anything():
    r = ScriptRunner(network())
    before = r.net.to_json()
    with pytest.raises(NetError, match="trace is a global setting"):
        r.run("mping all all * trace=maybe")
    assert r.log == [] and r.net.to_json() == before


def test_queued_trace_reaches_real_ui_and_plain_text_tee(tmp_path):
    path = tmp_path / "paths.log"

    async def go():
        app = ByteSimApp(network(), auto=False, log_file=path)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.run_command("trace on")
            app.run_command("detail off")
            app.run_command("pause")
            app.run_command("mping c1 c2")
            assert "TRACE" not in path.read_text(encoding="utf-8")
            app.run_command("resume")
            assert "TRACE REQUEST c1 → c2" in path.read_text(encoding="utf-8")
            assert "TRACE REPLY" not in path.read_text(encoding="utf-8")
            for _ in range(1000):
                if not app.canvas.busy:
                    break
                app.canvas._tick()
            assert not app.canvas.busy and not app.canvas.hot_links
            text = path.read_text(encoding="utf-8")
            assert "TRACE REPLY c2 → c1" in text and "-fabric#3->" in text
            assert "[b cyan]" not in text
            visible = " ".join(line.text for line in app.query_one("#log").lines)
            assert "TRACE REQUEST c1" in visible and "TRACE REPLY c2" in visible

    asyncio.run(go())
