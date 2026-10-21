"""State shared by every part of the network model, plus the basic lookups."""
from __future__ import annotations

from ..constants import KINDS, RANK
from .types import Device, Iface, Link, NetError


class NetworkBase:
    """Holds the network's state. The mixins in this package add behaviour on top."""

    _memo: dict | None = None  # populated only inside remembering()
    _route_memo: dict | None = None  # read-only results, only during a single route()

    def __init__(self) -> None:
        self.layout_mode = "layered"             # display arrangement; saved with positions
        self.group_orientation = "auto"          # layout direction; child groups inherit it
        self.devices: dict[str, Device] = {}
        self.links: list[Link] = []
        self.counters = {k: 0 for k in KINDS}
        self.groups: dict[str, list[str]] = {}    # group -> member device ids / group names
        self.group_layouts: dict[str, str] = {}   # group -> horizontal / vertical (absent: auto)
        self.ifaces: dict[str, Iface] = {}        # "c1:1" -> Iface
        self.dhcp_off: set[str] = set()           # routers/gateways not serving DHCP
        self.mac_tables: dict[str, dict[str, int]] = {}   # switch -> {mac: port}
        self.clusters: dict[str, str] = {}        # HA cluster (a group of routers) -> split policy
        self.cluster_modes: dict[str, str] = {}   # cluster -> "active-passive" (absent: active-active)
        self.lags: set[frozenset[str]] = set()    # bundles: pairs of ends (a device, or an MLAG domain)
        self.mlag_domains: set[str] = set()       # groups of switches that act as one logical switch
        self.internet: str | None = None          # the group that is the public internet (see wan.py)
        self.nat_off: set[str] = set()            # gateways facing the internet without NAT
        self.nat_hard: set[str] = set()           # NAT gateways that map per destination (hole punching fails)
        self.overlays: dict[str, dict] = {}       # overlay network name -> its state (see overlay.py)
        self.relays: list[str] = []                 # devices running a relay, in preference order
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
        i = self.link_index(dev_id, port)
        return self.links[i] if i is not None else None

    def link_index(self, dev_id: str, port: int) -> int | None:
        if self._memo is not None:
            if "ports" not in self._memo:
                ports = {}
                for i, l in enumerate(self.links):
                    ports.setdefault((l.a, l.pa), i)
                    ports.setdefault((l.b, l.pb), i)
                self._memo["ports"] = ports
            return self._memo["ports"].get((dev_id, port))
        for i, l in enumerate(self.links):
            if (l.a == dev_id and l.pa == port) or (l.b == dev_id and l.pb == port):
                return i
        return None

    def adjacent_links(self, dev_id: str) -> list[tuple[int, Link]]:
        """Cables touching a device, in original cable order. Liveness stays live."""
        if self._memo is None:
            return [(i, l) for i, l in enumerate(self.links) if l.touches(dev_id)]
        if "adjacency" not in self._memo:
            adjacency: dict[str, list[tuple[int, Link]]] = {}
            for i, l in enumerate(self.links):
                adjacency.setdefault(l.a, []).append((i, l))
                if l.b != l.a:
                    adjacency.setdefault(l.b, []).append((i, l))
            self._memo["adjacency"] = adjacency
        return self._memo["adjacency"].get(dev_id, [])

    def free_port(self, dev: Device) -> int | None:
        for p in range(1, dev.nports + 1):
            if self.link_on(dev.id, p) is None:
                return p
        return None

    def is_router(self, dev_id: str) -> bool:
        return self.devices[dev_id].kind in "rg"
