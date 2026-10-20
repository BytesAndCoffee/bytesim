"""Console tee preserves display output, async replies, and file resources."""
import asyncio

import pytest
from rich.markup import escape

from bytesim import NetError
from bytesim.cli import build_parser
from bytesim.scripting import ScriptRunner, replay
from bytesim.tui import ByteSimApp


def test_tee_appends_plain_unicode_text_and_can_stop_and_restart(tmp_path):
    path = tmp_path / "Packets [Test]=One.log"
    path.write_text("previous session\n", encoding="utf-8")
    with ScriptRunner() as r:
        r.run(f'tee "{path}"')
        stream = r._tee_stream
        r.log_msg("[green]reply ✓[/] " + escape("literal [value]"))
        assert path.read_text(encoding="utf-8").startswith("previous session\n")
        assert "reply ✓ literal [value]\n" in path.read_text(encoding="utf-8")
        r.run("tee")
        assert str(path) in r.log[-1] and "(append)" in r.log[-1]
        r.run("tee off")
        assert stream.closed and r.tee_path is None
        stopped = path.read_text(encoding="utf-8")
        r.log_msg("after stop")
        assert path.read_text(encoding="utf-8") == stopped
        r.run(f"tee {path}")
        stream = r._tee_stream
        r.log_msg("restarted")
    assert stream.closed
    assert path.read_text(encoding="utf-8").endswith("restarted\n")
    assert "[green]" not in path.read_text(encoding="utf-8")


def test_switching_files_and_failed_open_preserve_the_active_log(tmp_path):
    first, second = tmp_path / "First.log", tmp_path / "Second.log"
    with ScriptRunner() as r:
        r.run(f"tee {first}")
        old = r._tee_stream
        with pytest.raises(NetError, match="couldn't open console log"):
            r.run(f"tee {tmp_path / 'missing' / 'file.log'}")
        assert r._tee_stream is old and not old.closed
        r.log_msg("still first")
        r.run(f"tee {second}")
        assert old.closed
        r.log_msg("now second")
    assert "still first" in first.read_text(encoding="utf-8")
    assert "now second" not in first.read_text(encoding="utf-8")
    assert "now second" in second.read_text(encoding="utf-8")


def test_failed_write_disables_the_tee_and_reports_only_once(tmp_path):
    class BrokenFile:
        closed = False

        def write(self, text):
            raise OSError("disk full")

        def close(self):
            self.closed = True

    r = ScriptRunner()
    r.tee_path = tmp_path / "broken.log"
    failed = BrokenFile()
    r._tee_stream = failed
    r.log_msg("first message")
    r.log_msg("second message")
    assert r.log[0] == "first message" and r.log[-1] == "second message"
    assert sum("console tee disabled" in m for m in r.log) == 1
    assert "disk full" in r.log[1]
    assert failed.closed and r._tee_stream is None and r.tee_path is None


def test_relative_paths_and_disabled_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with ScriptRunner() as r:
        r.run("tee\ntee off")
        assert "console tee: off" in r.log[-1]
        r.run("tee Relative.log")
        assert r.tee_path == tmp_path / "Relative.log"
    assert (tmp_path / "Relative.log").exists()


def test_replay_closes_tee_and_log_state_is_independent_of_the_network(tmp_path):
    path = tmp_path / "replay.log"
    net = replay(f"tee {path}\nadd c\nclear\nadd r")
    assert "r1" in net.devices and "console" not in net.to_json()
    assert "+[/] router r1" not in path.read_text(encoding="utf-8")  # markup was stripped
    assert "router r1" in path.read_text(encoding="utf-8")
    # The runner context also closes on exceptions.
    with pytest.raises(NetError):
        with ScriptRunner() as r:
            r.run(f"tee {path}")
            stream = r._tee_stream
            r.run("unknown-command")
    assert stream.closed


def test_cli_log_file_option_preserves_filename_case_and_spaces():
    args = build_parser().parse_args(["--log-file", "My Traffic.log", "--demo", "ha"])
    assert str(args.log_file) == "My Traffic.log"


def test_real_app_tee_captures_later_replies_errors_and_closes_on_exit(tmp_path):
    path = tmp_path / "ui.log"

    async def go():
        app = ByteSimApp(log_file=path)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert "ByteSim" in path.read_text(encoding="utf-8")
            app.run_command("demo ha")
            before = app.net.to_json()
            app.run_command("tee")
            assert app.net.to_json() == before and len(app.undo_stack) == 1
            app.run_command("detail on")
            app.run_command("mping c1 c2 c6")
            for _ in range(1000):
                if not app.canvas.busy:
                    break
                app.canvas._tick()
            assert not app.canvas.busy
            app.run_command("unknown-command")
            contents = path.read_text(encoding="utf-8")
            assert "reply from c2" in contents and "reply from c6" in contents
            assert "multicast ping: 2/2 replies, 0 failed" in contents
            assert "✗ unknown command" in contents
            assert "[green]" not in contents and "\x1b[" not in contents
            visible = " ".join(line.text for line in app.query_one("#log").lines)
            assert "multicast ping: 2/2 replies" in visible and "unknown command" in visible
            stream = app._tee_stream
        assert stream.closed and app.tee_path is None

    asyncio.run(go())


def test_bad_startup_log_file_reports_error_without_breaking_app(tmp_path):
    async def go():
        app = ByteSimApp(log_file=tmp_path / "missing" / "ui.log")
        async with app.run_test(size=(100, 40)):
            text = " ".join(line.text for line in app.query_one("#log").lines)
            assert "couldn't open console log" in text and "ByteSim" in text
            assert app.tee_path is None

    asyncio.run(go())
