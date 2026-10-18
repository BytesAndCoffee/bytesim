"""Cabling macros, shared by YAML topologies and the console.

Every macro turns lists of cable ends into a list of (end, end) pairs, cabled in
that order. An end is (device id, port or None); None means the first free port,
so the order of the pairs decides which ports each cable gets.
"""
from __future__ import annotations

from typing import TypeVar

from .types import NetError

T = TypeVar("T")
MACROS = ("connect", "mesh", "zip", "parallel", "chain", "ring")


def macro_pairs(op: str, sides: list[list[T]], count: int | None = None) -> list[tuple[T, T]]:
    """Pairs for one macro.

    connect [a] [b] · mesh [A] [B] (every a with every b, a-major) · zip [A] [B] (pairwise)
    parallel [a] [b] + count · chain [x…] (x0–x1, x1–x2…) · ring [x…] (a chain, closed)."""
    if op in ("connect", "mesh", "zip", "parallel"):
        if len(sides) != 2:
            raise NetError(f"{op} takes two names")
        a, b = sides
        if op in ("connect", "parallel") and (len(a) != 1 or len(b) != 1):
            raise NetError(f"{op} joins single devices (use mesh or zip for sets)")
        if op == "connect":
            return [(a[0], b[0])]
        if op == "parallel":
            if not count or count < 1:
                raise NetError("parallel takes a count: parallel a b 2")
            return [(a[0], b[0])] * count
        if op == "mesh":
            return [(x, y) for x in a for y in b]
        if len(a) != len(b):
            raise NetError(f"zip needs equal sizes ({len(a)} vs {len(b)})")
        return list(zip(a, b))
    if op in ("chain", "ring"):
        seq = [x for side in sides for x in side]
        if len(seq) < 2:
            raise NetError(f"{op} needs at least two devices")
        return list(zip(seq, seq[1:])) + ([(seq[-1], seq[0])] if op == "ring" and len(seq) > 2 else [])
    raise NetError(f"unknown macro {op!r}")
