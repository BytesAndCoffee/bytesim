"""Construction scripts: export the network as console commands, and run script files."""
from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from ...model import NetError
from ...synthesis import MODES, synthesize
from .base import CommandHost
from .registry import command


class ScriptCommands(CommandHost):
    @command("run", "source")
    def cmd_run(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) != 1:
            raise NetError("usage: run <file>   (one console command per line; # starts a comment)")
        path = Path(args[0]).expanduser()
        try:
            text = path.read_text()
        except OSError as e:
            raise NetError(f"couldn't read {path}: {e}") from None
        ran = self.run_script(text, origin=str(path))  # type: ignore[attr-defined]
        self.log_msg(f"ran {ran} command{'s' if ran != 1 else ''} from {path}: "
                     f"{len(self.net.devices)} devices, {len(self.net.links)} cables, {len(self.net.groups)} groups")

    def export_script(self, args: list[str]) -> None:
        """export script [explicit|readable|shortest] [file]"""
        modes = [a for a in args if a.lower() in MODES]
        files = [a for a in args if a.lower() not in MODES]
        if len(modes) > 1 or len(files) > 1:
            raise NetError(f"usage: export script [{'|'.join(MODES)}] [file]   (no file: show it here)")
        mode = modes[0].lower() if modes else "readable"
        res = synthesize(self.net, mode)
        reuse = (f", reusing structure {len(res.used)} time{'s' if len(res.used) != 1 else ''} "
                 f"({res.explicit_commands} without)" if res.used else "")
        summary = f"{mode} script: {res.commands} commands{reuse}, verified by replaying it"
        if files:
            path = Path(files[0]).expanduser()
            try:
                path.write_text(res.script)
            except OSError as e:
                raise NetError(f"couldn't write {path}: {e}") from None
            self.log_msg(f"[green]exported {summary} → {path}[/]")
            self.log_msg(f"  [dim]replay it with: clear, then run {path}[/]")
        else:
            for line in res.script.splitlines():
                self.log_msg(f"[dim]{escape(line)}[/]" if line.startswith("#") else escape(line))
            self.log_msg(f"[green]{summary}[/]")
        for s in res.used:
            self.log_msg(f"  [cyan]{escape(s.command())}[/]  [dim]{escape(s.describe())}[/]")
        for text in res.limitations:
            self.log_msg(f"  [yellow]not expressible as a command: {escape(text)}[/]")
