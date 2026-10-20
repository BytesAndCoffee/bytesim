"""Session console output copied to an append-only text log."""
from __future__ import annotations

from pathlib import Path
from typing import TextIO

from rich.markup import escape
from rich.text import Text

from ...model import NetError
from .base import CommandHost
from .registry import command


class ConsoleCommands(CommandHost):
    tee_path: Path | None = None
    _tee_stream: TextIO | None = None

    def _output_setting(self, name: str, args: list[str], *, auto: bool = False) -> None:
        choices = ("on", "off", "auto") if auto else ("on", "off")
        if len(args) > 1 or (args and args[0].lower() not in choices):
            raise NetError(f"usage: {name} [{'|'.join(choices)}]")
        field = f"{name}_enabled"
        if args:
            value = args[0].lower()
            setattr(self, field, None if value == "auto" else value == "on")
        current = getattr(self, field)
        value = "auto" if current is None else "on" if current else "off"
        self.log_msg(f"{name}: [b]{value}[/]")
        self.refresh_sidebar()

    @command("trace")
    def cmd_trace(self, args: list[str], opts: dict[str, str]) -> None:
        self._output_setting("trace", args)

    @command("detail")
    def cmd_detail(self, args: list[str], opts: dict[str, str]) -> None:
        self._output_setting("detail", args, auto=True)

    @command("tee")
    def cmd_tee(self, args: list[str], opts: dict[str, str]) -> None:
        if not args:
            self.log_msg(f"console tee: {escape(str(self.tee_path))} (append)"
                         if self.tee_path else "[dim]console tee: off[/]")
            return
        if len(args) == 1 and args[0].lower() == "off":
            self.log_msg("[dim]console tee: off[/]")
            error = self.close_console_log()
            if error:
                raise NetError(error)
            return
        try:
            filename = " ".join(args)
            if len(filename) >= 2 and filename[0] in "\"'" and filename[-1] == filename[0]:
                filename = filename[1:-1]
            path = Path(filename).expanduser().absolute()
            if path == self.tee_path:
                self.log_msg(f"console tee already appending to {escape(str(path))}")
                return
            # Open first: a bad destination must leave the existing tee running.
            stream = path.open("a", encoding="utf-8", buffering=1)
        except (OSError, ValueError, RuntimeError) as e:
            raise NetError(f"couldn't open console log: {e}") from None
        error = self.close_console_log()
        self.tee_path, self._tee_stream = path, stream
        if error:
            self.log_msg(f"[red]✗ {escape(error)}[/]")
        self.log_msg(f"console tee: {escape(str(path))} (append)")

    def write_console_log(self, msg: str) -> str | None:
        """Return one warning on write failure, disabling the tee before reporting it."""
        if self._tee_stream is None:
            return None
        try:
            self._tee_stream.write(Text.from_markup(msg).plain + "\n")
        except (OSError, ValueError, UnicodeError) as e:
            path = self.tee_path
            self.close_console_log()
            return f"console tee disabled for {path}: {e}"
        return None

    def close_console_log(self) -> str | None:
        """Release the file on stop, switch, exit, or a failed write."""
        stream, path = self._tee_stream, self.tee_path
        self._tee_stream = self.tee_path = None
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError, UnicodeError) as e:
                return f"couldn't close console log {path}: {e}"
        return None
