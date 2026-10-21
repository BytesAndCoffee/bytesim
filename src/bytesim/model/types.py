"""Plain data: devices, cables, interface addresses."""
from __future__ import annotations

from dataclasses import dataclass

from ..constants import BOX_H, KINDS


class NetError(Exception):
    pass


def fmt(addr: tuple[int, int] | None) -> str:
    return f"({addr[0]},{addr[1]})" if addr else "—"


@dataclass
class Iface:
    """Logical address of one port/NIC: (network id, 8-bit host).

    mode: "dhcp" (computers' default), "adhoc" (pick a free host, no server),
          "static", "auto" (router/gateway ports), or "none".
    via:  how the current address was actually obtained.
    """
    mode: str
    net: int | None = None
    host: int | None = None
    gw: int | None = None       # gateway's host number, on the same network
    via: str = ""
    lease: float | None = None  # when a DHCP lease runs out (simulated clock)

    @property
    def addr(self) -> tuple[int, int] | None:
        return (self.net, self.host) if self.host is not None and self.net is not None else None

    def clear(self) -> None:
        self.net = self.host = self.gw = self.lease = None
        self.via = ""


Leg = tuple[tuple[str, int], tuple[str, int], list[tuple[str, int]]]


@dataclass
class Device:
    kind: str
    num: int
    nports: int
    x: int = 0
    y: int = 0
    up: bool = True

    @property
    def id(self) -> str:
        return f"{self.kind}{self.num}"

    @property
    def name(self) -> str:
        return KINDS[self.kind][0]

    @property
    def color(self) -> str:
        return KINDS[self.kind][2]

    @property
    def width(self) -> int:
        return max(len(self.id) + 2, self.nports + 2) + 2

    def port_x(self, port: int) -> int:
        inner = self.width - 2
        return self.x + 1 + (inner - self.nports) // 2 + (port - 1)

    def port_name(self, port: int) -> str:
        return f"nic{port}" if self.kind == "c" else f"p{port}"

    def mac(self, port: int) -> str:
        """Device id plus a port letter: R1A, R1B, ... Z, AA, AB ..."""
        letters, n = "", port
        while n:
            n, r = divmod(n - 1, 26)
            letters = chr(65 + r) + letters
        return self.id.upper() + letters

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + BOX_H


@dataclass
class Link:
    a: str
    pa: int
    b: str
    pb: int
    role: str = "data"       # data | control | control-backup | fabric
    up: bool = True

    @property
    def control(self) -> bool:
        return self.role.startswith("control")

    def touches(self, dev_id: str) -> bool:
        return dev_id in (self.a, self.b)

    def other(self, dev_id: str) -> tuple[str, int]:
        return (self.b, self.pb) if dev_id == self.a else (self.a, self.pa)

    def port_of(self, dev_id: str) -> int:
        return self.pa if dev_id == self.a else self.pb
