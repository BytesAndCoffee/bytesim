"""Where cables are drawn: orthogonal paths between ports, and the line glyphs."""
from __future__ import annotations

from ..constants import BOX_H
from ..model import Device, Link, Network

N, E, S, W = 1, 2, 4, 8

LINE = {
    0: " ", N: "│", S: "│", N | S: "│", E: "─", W: "─", E | W: "─",
    N | E: "└", N | W: "┘", S | E: "┌", S | W: "┐",
    N | S | E: "├", N | S | W: "┤", E | W | S: "┬", E | W | N: "┴",
    N | E | S | W: "┼",
}

LINE2 = {m: c.translate(str.maketrans("│─└┘┌┐├┤┬┴┼", "║═╚╝╔╗╠╣╦╩╬")) for m, c in LINE.items()}

LINE_HEAVY = {m: c.translate(str.maketrans("│─└┘┌┐├┤┬┴┼", "┃━┗┛┏┓┣┫┳┻╋")) for m, c in LINE.items()}
LINE_DASH = {m: c.translate(str.maketrans("│─", "┆┄")) for m, c in LINE.items()}   # STP-blocked


def dirbit(dx: int, dy: int) -> int:
    return {(0, -1): N, (1, 0): E, (0, 1): S, (-1, 0): W}[(dx, dy)]


def _polyline(corners: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pts = [corners[0]]
    for x2, y2 in corners[1:]:
        x, y = pts[-1]
        while (x, y) != (x2, y2):
            x += (x2 > x) - (x2 < x)
            y += (y2 > y) - (y2 < y)
            pts.append((x, y))
    return pts

def _zpath(s: tuple[int, int], e: tuple[int, int], idx: int) -> list[tuple[int, int]]:
    (sx, sy), (ex, ey) = s, e
    gap = ey - sy
    mid = sy + 1 + idx % (gap - 1) if gap >= 2 else sy
    return _polyline([(sx, sy), (sx, mid), (ex, mid), (ex, ey)])

def _bus_slot(net: Network, dev: Device) -> int:
    row = sorted(d.x for d in net.devices.values() if d.y == dev.y)
    return row.index(dev.x)

def route(net: Network, link: Link, idx: int) -> list[tuple[int, int]]:
    """Cells a cable occupies, ordered from link.a to link.b."""
    A, B = net.devices[link.a], net.devices[link.b]
    ax, bx = A.port_x(link.pa), B.port_x(link.pb)
    # Cables leaving the same upper device share a horizontal "bus" row;
    # neighbouring devices use different rows so their buses don't merge.
    if B.y >= A.y + BOX_H + 1:      # B below A
        return _zpath((ax, A.y + BOX_H), (bx, B.y - 1), _bus_slot(net, A))
    if A.y >= B.y + BOX_H + 1:      # B above A
        return _zpath((bx, B.y + BOX_H), (ax, A.y - 1), _bus_slot(net, B))[::-1]
    # Overlapping rows: loop underneath both boxes.
    y = max(A.y, B.y) + BOX_H + 1 + idx % 2
    return _polyline([(ax, A.y + BOX_H), (ax, y), (bx, y), (bx, B.y + BOX_H)])
