"""End-to-end: drive the real Textual app headlessly."""
import asyncio

from bytesim.tui import ByteSimApp


async def _idle(app, pilot, limit=30.0):
    """Wait until every packet animation has finished."""
    waited = 0.0
    await pilot.pause(0.05)
    while app.canvas.busy and waited < limit:
        await pilot.pause(0.2)
        waited += 0.2


async def _session(commands):
    app = ByteSimApp()
    async with app.run_test(size=(170, 60)) as pilot:
        for cmd in commands:
            box = app.query_one("#cmd")
            box.focus()
            box.value = cmd
            await pilot.press("enter")
            await _idle(app, pilot)
        log = [line.text for line in app.query_one("#log").lines]
        app.final_selection = (app.canvas.selected, app.canvas.selected_group)   # widgets die on exit
        return app, log


def test_demo_ping_and_multicast_complete():
    app, log = asyncio.run(_session(["demo", "ping c1 c8", "mcast c1 c4 c6"]))
    text = "\n".join(log)
    assert "reply from c8" in text
    assert "delivered to 2 receivers" in text
    assert "✗" not in text


def test_errors_are_reported_not_raised():
    app, log = asyncio.run(_session(["frobnicate", "r1 s1", "ping c1 c2"]))
    assert sum("✗" in line for line in log) == 3


def test_graph_mode_and_save_load(tmp_path):
    path = tmp_path / "net.json"
    app, log = asyncio.run(_session(["demo ha", "group dc1 fab1 rfab1", "mode graph",
                                     "fail r1 r2", "ping c1 c6", f"save {path}", "clear",
                                     f"load {path}", "groups"]))
    text = "\n".join(log)
    assert "reply from c6" in text
    assert "▣ dc1" in text and path.exists()


def test_auto_renumber_follows_every_change():
    app, log = asyncio.run(_session(["demo", "renumber auto dfs", "del s1", "add c", "s2 c11",
                                     "renumber off", "add c"]))
    text = "\n".join(log)
    assert "renumbered (DFS)" in text and "✗" not in text
    net = app.net
    assert sorted(d for d in net.devices if d.startswith("s")) == ["s1", "s2"]
    # Everything except the computer added after `renumber off` is numbered in DFS order:
    # walking the DFS, each kind's ids come out as 1, 2, 3 …
    last = f"c{net.counters['c']}"
    seen: dict[str, int] = {}
    for did in net.traversal("dfs"):
        if did != last:
            seen[did[0]] = seen.get(did[0], 0) + 1
            assert did == f"{did[0]}{seen[did[0]]}"
    # with the mode off, the new computer simply took the next number
    assert last in net.devices and net.counters["c"] == seen["c"] + 1


def test_storm_then_stp_fixes_it():
    app, log = asyncio.run(_session(["demo loop", "ping c1 c2", "ping c1 c3", "stp on", "ping c1 c3"]))
    text = " ".join(" ".join(log).split())       # the console wraps long lines
    assert "BROADCAST STORM" in text and "broadcast storm on s1 s2 s3" in text
    assert "storm over" in text and "reply from c3" in text
    assert not app.net.storming


def test_clone_command():
    app, log = asyncio.run(_session(["demo ha", "clone rfab1", "clone fab1 lab", "clone nope", "groups"]))
    text = " ".join(" ".join(log).split())
    assert "cloned rfab1 → rfab2" in text and "cloned fab1 → lab" in text
    assert "usage: clone" in text
    assert app.net.clusters.get("rfab2") == "none"      # the copy is an HA cluster too
    assert app.final_selection == (None, "lab")


def test_export_and_load_topology(tmp_path):
    path = tmp_path / "net.yaml"
    app, log = asyncio.run(_session(["demo ha", f"export {path} ports", "clear", f"load {path}", "ping c1 c6"]))
    text = " ".join(" ".join(log).split())
    assert "every port pinned" in text and "topology: auto-arranged" in text and "reply from c6" in text
    assert app.auto and path.read_text().startswith("schema: bytesim/topology-v1")


def test_auto_numbered_group_names_in_commands():
    app, log = asyncio.run(_session(["demo ha", "group fab(1) c1 c2", "clone fab1 fab(1)"]))
    text = " ".join(" ".join(log).split())
    assert "▣ group fab3" in text and "cloned fab1 → fab4" in text


def test_tokens_keep_parentheses_together():
    assert ByteSimApp.tokens("copy link1(rfab1, rfab2)  link2( rfab2 ,rfab3 ) ports") == \
        ["copy", "link1(rfab1,rfab2)", "link2(rfab2,rfab3)", "ports"]
    assert ByteSimApp.tokens("group fab(1) s1") == ["group", "fab(1)", "s1"]


def test_ports_copy_command():
    app, log = asyncio.run(_session([
        "add r 6 6", "add s 5 4", "group a r1 r2", "group b r3 r4", "group c r5 r6",
        "group t s1 s2", "group t2 s3 s4",
        "s1 s2", "r1 s1", "r2 s2", "r3 s1", "r4 s2",
        "ports copy t(a, b) t2(b, c)",          # cables only, onto the existing t2
        "ports copy t(a, b) t3(b, c)",          # t3 doesn't exist: points at `copy`
        "copy t(a, b) t(1)(b, c)"]))            # creates the group (t1) and cables it
    text = " ".join(" ".join(log).split())
    assert "t2 now cabled like t (a→b, b→c): 4 cables made" in text
    assert "copy t(a,b) t3(b,c)" in text
    assert "cloned t → t1" in text and "4 rewired to b c" in text


def test_export_script_then_run_rebuilds(tmp_path):
    from bytesim.model.equivalence import differences
    path = tmp_path / "net.txt"
    holder = {}

    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(170, 60)) as pilot:
            for cmd in ["demo ha", "fail r1:5", f"export script shortest {path}", "export script explicit",
                        "clear", f"run {path}"]:
                if cmd == "clear":
                    from bytesim.model import Network
                    holder["before"] = Network.from_json(app.net.to_json())
                box = app.query_one("#cmd")
                box.focus()
                box.value = cmd
                await pilot.press("enter")
                await _idle(app, pilot)
            holder["after"] = app.net
            return [line.text for line in app.query_one("#log").lines]

    log = asyncio.run(go())
    text = " ".join(" ".join(log).split())
    assert "exported shortest script" in text and "verified by replaying it" in text
    assert "add r 6 2" in text                               # the explicit one was printed in the console
    assert "ran " in text and differences(holder["before"], holder["after"]) == []


def test_run_reports_the_failing_line(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("add s 4 1\n# comment\nfrobnicate\n")
    app, log = asyncio.run(_session([f"run {path}"]))
    text = " ".join(" ".join(log).split())
    assert "line 3 (frobnicate)" in text and "unknown command" in text


def test_undo_and_redo_many_levels():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(170, 60)) as pilot:
            async def run(cmd):
                box = app.query_one("#cmd")
                box.focus()
                box.value = cmd
                await pilot.press("enter")
                await _idle(app, pilot)

            seen = {}
            await run("demo loop")
            seen["demo"] = (len(app.net.devices), len(app.net.links))
            await run("add r 4 1")
            await run("r2 s3")
            await run("list")                       # read-only: no step
            await run("ping c1 r1")                 # a storm (STP is off in the loop demo): a step
            seen["storm"] = bool(app.net.storming)
            await run("history")
            await run("undo")                       # the storm is gone
            seen["after storm undo"] = bool(app.net.storming)
            await run("undo 2")                     # the cable, then r2
            seen["back to demo"] = (len(app.net.devices), len(app.net.links))
            await run("redo")                       # r2 is back
            seen["redo"] = "r2" in app.net.devices
            await run("clear")
            await run("undo")                       # even clear can be undone
            seen["clear undone"] = len(app.net.devices)
            await run("add s 4 1")                  # a new change drops the redo stack
            await run("redo")
            # a burst of moves is one step
            app.canvas.select("s1")
            app.canvas.focus()
            start, x = len(app.undo_stack), app.net.devices["s1"].x
            for _ in range(3):
                await pilot.press("right")
            moved = len(app.undo_stack)
            await pilot.press("u")
            seen["moves"] = (start, moved, len(app.undo_stack), app.net.devices["s1"].x == x)
            log = " ".join(" ".join(l.text for l in app.query_one("#log").lines).split())
            return seen, log

    seen, log = asyncio.run(go())
    assert seen["storm"] and not seen["after storm undo"]
    assert seen["back to demo"] == seen["demo"] and seen["redo"]
    assert seen["clear undone"] == seen["demo"][0] + 1
    assert "nothing to redo" in log and "undid" in log
    start, moved, undone, back_in_place = seen["moves"]
    assert moved == start + 1 and undone == start and back_in_place   # three moves, one step


def test_a_failed_script_is_one_undo_step(tmp_path):
    path = tmp_path / "half.txt"
    path.write_text("add s 4 2\ns1 s2\nfrobnicate\n")
    app, log = asyncio.run(_session([f"run {path}", "undo"]))
    assert app.net.devices == {}                             # the half-built network is gone


def test_traffic_command():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(170, 60)) as pilot:
            async def run(cmd, wait=0.1):
                box = app.query_one("#cmd")
                box.focus()
                box.value = cmd
                await pilot.press("enter")
                await pilot.pause(wait)

            await run("demo ha")
            steps = len(app.undo_stack)
            await run("traffic on 20 seed=5", wait=2.5)
            sent = app.traffic.stats.sent
            await run("traffic rate 2")
            await run("traffic")
            await run("traffic off")
            await run("traffic off")
            await run("traffic on 99")
            await run("traffic on 2 hosts=nope")
            log = " ".join(" ".join(l.text for l in app.query_one("#log").lines).split())
            return sent, steps, len(app.undo_stack), app.traffic, log

    sent, before, after, gen, log = asyncio.run(go())
    assert sent > 10 and gen is None
    assert after == before                       # traffic doesn't add undo steps
    assert "traffic rate: 2 packets/s" in log and "traffic stopped" in log and "busiest cables" in log
    assert "already off" in log and "at most 50" in log and "no group nope" in log


def test_cluster_mode_command():
    app, log = asyncio.run(_session(["demo core", "clusters", "cluster core mode=active-active",
                                     "cluster core mode=sideways"]))
    text = " ".join(" ".join(log).split())
    assert "r2 standby" in text and "every active member forwards" in text and "mode must be" in text
    assert app.net.cluster_modes == {}


def test_lag_commands():
    app, log = asyncio.run(_session(["add s 6 2", "s1 s2", "s1 s2", "lag s1 s2", "lags", "lag s1 s2",
                                     "unlag s1 s2", "lag s1 s9"]))
    text = " ".join(" ".join(log).split())
    assert "2 cables act as one link" in text and "2/2 members carrying traffic" in text
    assert "already a link aggregation" in text and "separate cables again" in text
    assert app.net.lags == set()



def test_lag_detection_and_auto():
    app, log = asyncio.run(_session(["add g 4 1", "add s 6 2", "g1 s1", "g1 s1", "s1 s2", "s1 s2",
                                     "lag", "lag auto", "lags", "lag"]))
    text = " ".join(" ".join(log).split())
    assert "s1–g1 now has parallel cables: lag s1 g1 to aggregate them" in text
    assert "2 aggregatable spans" in text and "g1 treats them as one interface" in text
    assert app.net.lags == {frozenset({"s1", "g1"}), frozenset({"s1", "s2"})}
    assert "nothing to aggregate" in text


def test_mlag_commands():
    app, log = asyncio.run(_session(["add g 4 1", "add s 6 2", "group fab s1 s2", "g1 s1", "g1 s2",
                                     "mlag fab", "s1 s2", "mlag fab", "lag", "lag g1 fab", "mlags"]))
    text = " ".join(" ".join(log).split())
    assert "must be joined by peer links" in text and "act as one switch, 1 peer link" in text
    assert "g1–fab: 2 cables can now be one link" in text and "fab–g1: 2 parallel cables" in text
    assert "bundles to g1" in text and app.net.lags == {frozenset({"fab", "g1"})}
