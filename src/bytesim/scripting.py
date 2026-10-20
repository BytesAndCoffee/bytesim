"""Run ByteSim command scripts without the UI.

The runner uses the *same* command handlers and interpreter as the app; only the
screen is replaced by a stub. It builds structure (devices, groups, cables,
settings) and skips what the screen and the address model would do after each
command, which a script doesn't need to reproduce its structure.

    from bytesim.scripting import replay
    net = replay(open("site.txt").read())
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from rich.markup import escape

from .model import NetError, Network
from .tui.commands.addressing import AddressCommands
from .tui.commands.console import ConsoleCommands
from .tui.commands.groups import GroupCommands
from .tui.commands.interpreter import Interpreter
from .tui.commands.naming import NamingCommands
from .tui.commands.registry import collect
from .tui.commands.switching import SwitchingCommands
from .tui.commands.topology import TopologyCommands
from .tui.commands.traffic import TrafficCommands


class _Screen:
    """Stands in for the canvas: remembers the selection, finishes animations at once."""

    def __init__(self) -> None:
        self.selected: str | None = None
        self.selected_group: str | None = None
        self.pan = (0, 0)
        self.packets: list = []
        self.flash: dict = {}
        self.hot_links: Counter = Counter()
        self.size = SimpleNamespace(width=120, height=40)
        self.busy = False

    def select(self, dev_id: str | None) -> None:
        self.selected, self.selected_group = dev_id, None

    def select_group(self, name: str) -> None:
        self.selected, self.selected_group = None, name

    def send_packet(self, cells, color, on_done=None) -> None:
        if on_done:
            on_done()

    def focus(self, *a) -> None: ...
    def animate(self) -> None: ...
    def refresh(self, *a, **k) -> None: ...
    def reset(self) -> None: ...
    def heat(self, links) -> None: ...
    def cool(self, links) -> None: ...
    def flash_device(self, *a, **k) -> None: ...


class ScriptRunner(ConsoleCommands, TopologyCommands, GroupCommands, AddressCommands, TrafficCommands, NamingCommands,
                   SwitchingCommands, Interpreter):
    """A headless ByteSim: feed it commands, read back ``net``."""

    def __init__(self, network: Network | None = None) -> None:
        self.net = network or Network()
        self.auto = False
        self.graph = False
        self.replaying = 1            # always "in a script": no per-command side effects
        self.headless = True
        self.naming = None
        self.log: list[str] = []
        self._ha_seen: dict = {}
        self._screen = _Screen()
        self._commands = collect(type(self))

    @property
    def canvas(self):  # type: ignore[override]
        return self._screen

    def log_msg(self, msg: str) -> None:
        self.log.append(msg)
        error = self.write_console_log(msg)
        if error:
            self.log_msg(f"[red]✗ {escape(error)}[/]")

    def close(self) -> None:
        self.close_console_log()

    def __enter__(self) -> "ScriptRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def changed(self, skip=frozenset()) -> None:   # structure only: no layout, no re-addressing
        pass

    def refresh_sidebar(self) -> None:
        pass

    def replace_network(self, net: Network, auto: bool) -> None:
        self.net = net
        self.pending_actions.clear()

    def arrange(self, announce: bool = False) -> None:
        pass

    def exit(self) -> None:
        self.close()

    def run(self, text: str, origin: str = "script") -> Network:
        """Run every command; raises NetError naming the line that failed."""
        for n, line in self.script_lines(text):
            try:
                self.dispatch(self.tokens(line))
            except NetError as e:
                raise NetError(f"{origin}, line {n} ({line}): {e}") from None
        return self.net


def replay(text: str, network: Network | None = None) -> Network:
    """Build a network by running a script (into a fresh network unless one is given)."""
    with ScriptRunner(network) as runner:
        return runner.run(text)
