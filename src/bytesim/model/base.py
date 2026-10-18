"""State shared by every part of the network model, plus the basic lookups."""
from __future__ import annotations

from ..constants import KINDS, RANK
from .types import Device, Iface, Link, NetError


class NetworkBase:
    """Holds the network's state. The mixins in this package add behaviour on top."""

    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.links: list[Link] = []
        self.counters = {k: 0 for k in KINDS}
        self.groups: dict[str, list[str]] = {}    # group -> member device ids / group names
        self.ifaces: dict[str, Iface] = {}        # "c1:1" -> Iface
        self.dhcp_off: set[str] = set()           # routers/gateways not serving DHCP
        self.mac_tables: dict[str, dict[str, int]] = {}   # switch -> {mac: port}
        self.clusters: dict[str, str] = {}        # HA cluster (a group of routers) -> split policy
        self.cluster_modes: dict[str, str] = {}   # cluster -> "active-passive" (absent: active-active)
        self.notes: list[str] = []                # remarks from the last route() for the console
        self.stp_off: set[str] = set()            # switches with spanning tree switched off
        self.stp_priority: dict[str, int] = {}    # bridge priority (default 32768; lower = root)
        self.stp_blocked: set[tuple[str, int]] = set()    # (switch, port) held blocking by STP
        self.stp_roots: dict[str, str] = {}       # switch -> its area's root bridge
        self.stp_root_ports: dict[str, int] = {}  # switch -> port toward the root
        self.storming: set[str] = set()           # switches drowning in a broadcast storm
        self.mac_flaps: dict[str, dict[str, set[int]]] = {}  # switch -> mac -> ports it flapped between

    def get(self, dev_id: str) -> Device:
        try:
            return self.devices[dev_id.lower()]
        except KeyError:
            raise NetError(f"no such device: {dev_id}") from None

    def sorted_devices(self) -> list[Device]:
        return sorted(self.devices.values(), key=lambda d: (RANK[d.kind], d.num))

    def link_on(self, dev_id: str, port: int) -> Link | None:
        for l in self.links:
            if (l.a == dev_id and l.pa == port) or (l.b == dev_id and l.pb == port):
                return l
        return None

    def link_index(self, dev_id: str, port: int) -> int | None:
        for i, l in enumerate(self.links):
            if (l.a == dev_id and l.pa == port) or (l.b == dev_id and l.pb == port):
                return i
        return None

    def free_port(self, dev: Device) -> int | None:
        for p in range(1, dev.nports + 1):
            if self.link_on(dev.id, p) is None:
                return p
        return None

    def is_router(self, dev_id: str) -> bool:
        return self.devices[dev_id].kind in "rg"
