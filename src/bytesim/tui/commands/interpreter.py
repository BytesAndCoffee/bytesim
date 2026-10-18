"""The command interpreter: tokens → handler. Shared by the app and the headless runner."""
from __future__ import annotations

from ...constants import REF
from ...model import NetError
from .base import CommandHost


class Interpreter(CommandHost):
    _commands: dict[str, str]

    @staticmethod
    def tokens(line: str) -> list[str]:
        """Split on spaces, except inside parentheses: link1(rfab1, rfab2) is one token."""
        out, cur, depth = [], "", 0
        for ch in line:
            depth += (ch == "(") - (ch == ")")
            if ch.isspace() and depth <= 0:
                if cur:
                    out.append(cur)
                cur, depth = "", 0
            elif not (ch.isspace() and depth > 0):
                cur += ch
        if depth > 0:
            raise NetError(f"unclosed parenthesis in {cur!r}")
        return out + ([cur] if cur else [])

    def dispatch(self, toks: list[str]) -> None:
        """Route tokens to a handler. Shortcuts: ``r1`` selects, ``r1 s2`` cables,
        a bare group name selects the group. ``key=value`` tokens become options."""
        if not toks:
            return
        opts = {k.lower(): v.lower() for k, _, v in (t.partition("=") for t in toks if "=" in t)}
        toks = [t for t in toks if "=" not in t]
        if not toks:
            raise NetError("missing command")
        word, args = toks[0].lower(), toks[1:]

        if REF.match(toks[0]):
            if len(toks) == 1:
                did, _ = self._ref(toks[0])
                self.net.get(did)
                self.canvas.select(did)
                self.canvas.focus()
                return
            if len(toks) != 2:
                raise NetError("to cable devices type two ids, e.g. r1 s2")
            word, args = "link", toks
        elif word not in self._commands and word in self.net.groups and not args:
            word, args = "group", [word]

        handler = self._commands.get(word)
        if handler is None:
            raise NetError(f"unknown command {word!r} — type help")
        getattr(self, handler)(args, opts)

    @staticmethod
    def script_lines(text: str) -> list[tuple[int, str]]:
        """(line number, command) for every non-blank line, with # comments removed."""
        out = []
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if line:
                out.append((n, line))
        return out

    def run_script(self, text: str, origin: str = "script") -> int:
        """Run a script's commands in order; stop at the first error, saying where.

        While a script runs, the host may batch its usual per-command work (redraws,
        re-addressing, DHCP animations) until the end. Returns how many commands ran."""
        self.replaying += 1
        ran = 0
        try:
            for n, line in self.script_lines(text):
                try:
                    self.dispatch(self.tokens(line))
                except NetError as e:
                    raise NetError(f"{origin}, line {n} ({line}): {e}") from None
                ran += 1
        finally:
            self.replaying -= 1
            if not self.replaying:
                self.changed()
        return ran
