"""A tiny command registry: decorate a handler with the words that invoke it."""
from __future__ import annotations

from typing import Callable

Handler = Callable[..., None]


def command(*names: str) -> Callable[[Handler], Handler]:
    """Mark a method as the handler for one or more command words.

    Handlers are called as ``handler(self, args, opts)`` where ``args`` are the
    remaining tokens and ``opts`` holds any ``key=value`` tokens (lower-cased).
    """
    def mark(fn: Handler) -> Handler:
        fn.__commands__ = names  # type: ignore[attr-defined]
        return fn
    return mark


def collect(cls: type) -> dict[str, str]:
    """Map every command word to the name of the method that handles it."""
    table: dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        for attr, fn in vars(klass).items():
            for word in getattr(fn, "__commands__", ()):
                table[word] = attr
    return table
