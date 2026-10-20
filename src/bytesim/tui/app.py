"""The ByteSim Textual app: layout, the command line, and keeping everything in sync."""
from __future__ import annotations

import gc
from pathlib import Path

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Input, RichLog, Static

from .. import __version__
from ..model import NetError, Network
from .canvas import NetCanvas
from .command_input import CommandInput
from .commands.addressing import AddressCommands
from .commands.console import ConsoleCommands
from .commands.groups import GroupCommands
from .commands.history import HistoryCommands
from .commands.interpreter import Interpreter
from .commands.naming import NamingCommands
from .commands.scripting import ScriptCommands
from .commands.switching import SwitchingCommands
from .commands.registry import collect
from .commands.topology import TopologyCommands
from .commands.traffic import TrafficCommands
from .inspector import render_inspector


class ByteSimApp(ConsoleCommands, TopologyCommands, GroupCommands, AddressCommands, TrafficCommands, NamingCommands,
                 SwitchingCommands, ScriptCommands, HistoryCommands, Interpreter, App):
    """Canvas on the left, inspector on the right, console and command line below."""

    TITLE = "ByteSim"
    CSS = """
    #main { height: 1fr; }
    NetCanvas { width: 1fr; height: 1fr; border: round $panel-lighten-2; }
    NetCanvas:focus { border: round $accent; }
    #side { width: 38; border: round $panel-lighten-2; padding: 0 1; }
    #log { height: 9; border: round $panel-lighten-2; padding: 0 1; }
    #cmd { border: tall $panel-lighten-2; }
    #cmd:focus { border: tall $accent; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]
    AUTO_FOCUS = "#cmd"

    def __init__(self, network: Network | None = None, startup: list[str] | None = None,
                 auto: bool | None = None, log_file: str | Path | None = None,
                 trace: bool = False, detail: bool | None = None) -> None:
        """``network`` starts the app on an existing model; ``startup`` commands run once mounted.
        ``auto`` turns auto-arrange on or off (default: on, unless a network with its own
        positions is given). ``log_file`` appends console output from startup;
        ``trace`` and ``detail`` initialize session output settings."""
        super().__init__()
        self.net = network or Network()
        self.auto = (network is None) if auto is None else auto
        self.graph = False              # graph-only mode: connectivity without addressing
        self.trace_enabled = trace
        self.detail_enabled = detail
        self._startup = startup or []
        self._log_file = log_file
        self._ha_seen: dict = {}
        self._commands = collect(type(self))

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield NetCanvas(id="canvas")
            with VerticalScroll(id="side"):
                yield Static(id="info")
        yield RichLog(id="log", markup=True, wrap=True)
        yield CommandInput(id="cmd", placeholder="r1 s2 to cable · add r 8 · ping c1 c4 · help")
        yield Footer()

    @property
    def canvas(self) -> NetCanvas:
        return self.query_one(NetCanvas)

    def on_mount(self) -> None:
        self.query_one("#canvas").border_title = "network"
        self.query_one("#side").border_title = "inspector"
        self.query_one("#log").border_title = "console"
        if self._log_file is not None:
            try:
                self.cmd_tee([str(self._log_file)], {})
            except NetError as e:
                self.log_msg(f"[red]✗ {escape(str(e))}[/]")
        self.log_msg(f"[b]ByteSim {__version__}[/] — type [cyan]help[/] for commands, "
                     "or [cyan]demo[/] / [cyan]demo ha[/] for a sample network.")
        if self.net.devices:
            self.call_after_refresh(self.changed)
        for line in self._startup:
            self.call_after_refresh(self.run_command, line)
        self.refresh_sidebar()
        self.call_after_refresh(self.settle_heap)

    def settle_heap(self) -> None:
        """Collect once, then exempt what survives (widgets, the network) from later
        collections: full collections otherwise rescan it all and stall animation."""
        gc.unfreeze()                        # release what an earlier network left behind
        gc.collect()
        gc.freeze()

    # ---- shared helpers (used by the canvas and the command mixins) ----
    def on_unmount(self) -> None:
        self.close_console_log()

    def log_msg(self, msg: str) -> None:
        if self.replaying:                   # a script is running: keep the console quiet
            return
        self.query_one(RichLog).write(msg)
        error = self.write_console_log(msg)
        if error:
            self.log_msg(f"[red]✗ {escape(error)}[/]")

    def action_focus_cmd(self) -> None:
        self.query_one(CommandInput).focus()

    def action_focus_canvas(self) -> None:
        self.canvas.focus()

    def arrange(self, announce: bool = False) -> None:
        self.auto = True
        self.canvas.pan = (0, 0)
        if announce:
            self.log_msg(f"[green]auto-arrange on[/] · layout: [b]{self.net.layout_mode}[/]"
                         + f" · orientation: [b]{self.net.group_orientation}[/]")
        self.changed()

    def manual_moved(self) -> None:
        self.canvas.cancel_packets()
        if self.auto:
            self.auto = False
            self.log_msg("[dim]auto-arrange paused — press [b]a[/b] or type [b]auto[/b] to re-arrange[/]")

    def replace_network(self, net: Network, auto: bool) -> None:
        """Swap in a whole new network (clear, demo, load)."""
        self.net = net
        self.pending_actions.clear()
        self.auto = auto
        self._ha_seen = {}
        self.canvas.reset()
        self.changed()
        self.call_after_refresh(self.settle_heap)

    def changed(self, skip: frozenset[tuple[str, int]] = frozenset()) -> None:
        """Call after any structural change: re-address, renumber, report HA, re-layout, redraw."""
        if self.replaying:                   # a script is running: do it all once at the end
            return
        self.reconcile(skip)
        self.auto_renumber()
        if not self.graph:
            self._ha_report()
        if self.auto:
            self.net.auto_layout(self.canvas.size.width or 120, height=self.canvas.size.height or 40)
        c = self.canvas
        if c.cancel_if_changed():
            self.log_msg("[dim]in-flight packet work cancelled after network/layout change[/]")
        if c.selected not in self.net.devices:
            c.selected = None
        if c.selected_group not in self.net.groups:
            c.selected_group = None
        if not c.busy:
            c.hot_links.clear()
        c.refresh()
        self.refresh_sidebar()

    def on_resize(self, event: events.Resize) -> None:
        if self.auto:
            self.call_after_refresh(self.changed)

    def refresh_sidebar(self) -> None:
        c = self.canvas
        self.query_one("#info", Static).update(
            render_inspector(self.net, c.selected, c.selected_group, self.graph, self.auto,
                             self.naming_text(), self.traffic, paused=self.paused,
                             queued=len(self.pending_actions), preparing=c.preparing,
                             trace=self.trace_enabled, detail=self.detail_enabled))

    # ---- the command line ----------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        inp = self.query_one(CommandInput)
        inp.remember(line)
        inp.value = ""
        if line:
            self.log_msg(f"[dim]›[/] {escape(line)}")
            self.run_command(line)

    def run_command(self, line: str) -> None:
        """Run one command line, reporting errors in the console. Anything it changes
        (even partly, before an error) becomes one undo step."""
        try:
            self.recorded(line, lambda: self.dispatch(self.tokens(line)))
        except NetError as e:
            self.log_msg(f"[red]✗ {escape(str(e))}[/]")
