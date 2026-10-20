"""Packet pacing stays fast on large layouts and preserves multicast timing."""
import asyncio

import pytest

from bytesim import NetError
from bytesim.scripting import ScriptRunner
from bytesim.tui import ByteSimApp
from bytesim.tui.canvas import ANIMATION_FPS, PACKET_FRAMES, TRAIL


def test_long_paths_finish_quickly_and_shared_requests_stay_in_step():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(100, 40)):
            canvas = app.canvas
            for length in (1, 20, 335, 10000):
                completed = []
                canvas.send_packet([(i, 0) for i in range(length)], "red", lambda: completed.append(True))
                ticks = 0
                for _ in range(PACKET_FRAMES):
                    canvas._tick()
                    ticks += 1
                    if not canvas.busy:
                        break
                assert completed == [True] and not canvas.busy, length
                seconds = ticks / ANIMATION_FPS
                if length >= 335:
                    assert seconds == pytest.approx(1.5, abs=1 / 60)
                else:
                    assert seconds == pytest.approx((length + TRAIL) / 45, abs=1 / 60)

            common = [(i, 0) for i in range(100)]
            canvas.send_packet(common + [(100, y) for y in range(10)], "magenta")
            canvas.send_packet(common + [(100, y) for y in range(30)], "magenta")
            for _ in range(10):
                canvas._tick()
                assert len(canvas.packets) == 2
                assert canvas.packets[0].pos == canvas.packets[1].pos
            canvas.reset()

    asyncio.run(go())


def test_speed_controls_playback_in_flight_and_preserves_rendering():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(100, 40)):
            canvas = app.canvas
            durations = {}
            for setting in ("slow", "normal", "fast", "2x"):
                app.run_command("speed " + setting)
                canvas.send_packet([(i, 0) for i in range(335)], "red")
                ticks = 0
                while canvas.busy and ticks < 3 * PACKET_FRAMES:
                    canvas._tick()
                    canvas.refresh()
                    canvas.render_line(0)  # fractional progress still indexes integer screen cells
                    ticks += 1
                assert not canvas.busy
                durations[setting] = ticks
            assert durations["slow"] > durations["normal"] > durations["2x"] > durations["fast"]
            assert durations["normal"] >= PACKET_FRAMES - 1

            app.run_command("speed slow")
            canvas.send_packet([(i, 0) for i in range(335)], "red")
            canvas._tick()
            before = canvas.packets[0].pos
            app.run_command("speed fast")
            canvas._tick()
            assert canvas.packets[0].pos - before > before
            canvas.reset()

    asyncio.run(go())


@pytest.mark.parametrize("setting", ("0", "-1", "11", "nan", "inf", "nope", "normal fast"))
def test_invalid_speeds_leave_the_current_preference_unchanged(setting):
    r = ScriptRunner()
    r.run("speed 0.75")
    with pytest.raises(NetError, match="usage: speed"):
        r.run("speed " + setting)
    assert r.animation_speed == 0.75


def test_speed_is_a_session_preference_and_query_is_read_only():
    r = ScriptRunner()
    r.run("speed slow\ndemo basic\nclear\nspeed")
    assert r.animation_speed == 0.5
    assert "animation speed: [b]0.5x" in r.log[-1]


def test_long_request_and_reply_callbacks_finish_within_two_animation_windows():
    async def go():
        app = ByteSimApp()
        async with app.run_test(size=(100, 40)):
            canvas = app.canvas
            cells = [(i, 0) for i in range(1000)]
            completed = []

            def arrived():
                canvas.send_packet(cells[::-1], "green", lambda: completed.append(True))

            canvas.send_packet(cells, "red", arrived)
            for _ in range(2 * PACKET_FRAMES):
                canvas._tick()
                if not canvas.busy:
                    break
            assert completed == [True] and not canvas.busy

    asyncio.run(go())
