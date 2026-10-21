"""Random background traffic: who talks to whom, how busy each cable is, what got through.

The generator only decides *what* to send. Sending it (finding the path,
animating it, setting off storms) is the UI's job, so the same traffic can be
driven headlessly in tests with a fixed seed.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from .network import Network
from .types import Link

TRAFFIC_KINDS = ("unicast", "internet", "broadcast")
DEFAULT_MIX = {"unicast": 0.75, "internet": 0.15, "broadcast": 0.10}
HALF_LIFE = 1.5            # seconds for a cable's load reading to halve
MAX_PER_STEP = 25          # never burst more packets than this in one step


@dataclass
class TrafficStats:
    sent: int = 0
    delivered: int = 0
    dropped: Counter = field(default_factory=Counter)        # reason -> packets
    kinds: Counter = field(default_factory=Counter)          # unicast / internet / broadcast
    cables: Counter = field(default_factory=Counter)         # "a:pA–b:pB" -> packets carried
    talkers: Counter = field(default_factory=Counter)        # device -> packets sent
    received: Counter = field(default_factory=Counter)       # device -> packets received
    forwarded: Counter = field(default_factory=Counter)      # device -> packets passed on
    drops_at: Counter = field(default_factory=Counter)       # device -> its packets that were dropped

    @property
    def in_flight(self) -> int:
        return self.sent - self.delivered

    def record(self, net: Network, kind: str, path: list[tuple[str, int]]) -> None:
        """A packet left along a path (device, link index) of devices."""
        self.sent += 1
        self.kinds[kind] += 1
        self.talkers[path[0][0]] += 1
        for dev, _ in path[1:-1]:
            self.forwarded[dev] += 1
        for _, li in path[1:]:
            self.cables[cable_name(net.links[li])] += 1

    def drop(self, src: str, kind: str, reason: str) -> None:
        self.kinds[kind] += 1
        self.dropped[reason] += 1
        self.drops_at[src] += 1

    def arrive(self, dev: str) -> None:
        self.delivered += 1
        self.received[dev] += 1


def cable_name(l: Link) -> str:
    return f"{l.a}:p{l.pa}–{l.b}:p{l.pb}"


def drop_reason(message: str) -> str:
    """Sort a path error into the reasons the statistics count."""
    m = message.lower()
    if "storm" in m:
        return "storm"
    if "nat" in m and "unsolicited" in m:
        return "NAT"
    if "no address" in m:
        return "no address"
    if " is down" in m:
        return "device down"
    return "unreachable"


class TrafficGenerator:
    """Picks random flows at a steady rate. Same seed, same flows."""

    def __init__(self, rate: float = 2.0, seed: int | None = None, hosts: set[str] | None = None,
                 mix: dict[str, float] | None = None) -> None:
        self.rate = rate
        self.seed = seed
        self.hosts = hosts                       # restrict senders/receivers (None: every computer)
        self.mix = dict(mix or DEFAULT_MIX)
        self.rng = random.Random(seed)
        self.stats = TrafficStats()
        self.load: dict[int, float] = {}        # id(link) -> recent packets, decaying
        self.elapsed = 0.0
        self._owed = 0.0

    def due(self, dt: float) -> int:
        """How many packets to send after dt more seconds (steady pacing, no bursts)."""
        self.elapsed += dt
        self._owed += self.rate * dt
        n = min(int(self._owed), MAX_PER_STEP)
        self._owed -= int(self._owed)
        return n

    def senders(self, net: Network) -> list[str]:
        out = []
        for d in net.sorted_devices():
            if d.kind != "c" or not d.up or (self.hosts is not None and d.id not in self.hosts):
                continue
            if any(net.link_index(d.id, p) is not None for p in range(1, d.nports + 1)):
                out.append(d.id)
        return out

    def pick(self, net: Network) -> tuple[str, str, str] | None:
        """(kind, source, destination) for the next packet; destination is a device id,
        'internet', or '*' for a broadcast. None if nobody can send."""
        hosts = self.senders(net)
        if not hosts:
            return None
        kinds = [k for k in TRAFFIC_KINDS if self.mix.get(k, 0) > 0]
        if not any(d.kind == "g" for d in net.devices.values()):
            kinds = [k for k in kinds if k != "internet"] or kinds
        if len(hosts) < 2:
            kinds = [k for k in kinds if k != "unicast"] or kinds
        kind = self.rng.choices(kinds, weights=[self.mix[k] for k in kinds])[0]
        src = self.rng.choice(hosts)
        if kind == "unicast":
            others = [h for h in hosts if h != src]
            if not others:
                return None
            return kind, src, self.rng.choice(others)
        return kind, src, ("internet" if kind == "internet" else "*")

    # ---- load on each cable, for the heat shading ----------------------------
    def carry(self, net: Network, path: list[tuple[str, int]]) -> None:
        for _, li in path[1:]:
            key = id(net.links[li])
            self.load[key] = self.load.get(key, 0.0) + 1.0

    def cool(self, dt: float) -> None:
        factor = 0.5 ** (dt / HALF_LIFE)
        for key in list(self.load):
            self.load[key] *= factor
            if self.load[key] < 0.05:
                del self.load[key]

    def level(self, link: Link) -> float:
        """0 (idle) … 1 (the busiest cable right now, or at least 3 recent packets)."""
        here = self.load.get(id(link), 0.0)
        return min(1.0, here / max(3.0, max(self.load.values(), default=0.0)))
