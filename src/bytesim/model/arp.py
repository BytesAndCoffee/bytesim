"""ARP caches: what each interface has learned about its neighbours' MACs.

An interface asks once, by broadcast, and remembers the answer for ARP_AGE
seconds. Both ends learn from an exchange: the asker the answer, the target the
asker (from the request). Entries are never checked against the truth: if the
address has moved, frames go to the old MAC and are lost, and the asker drops
the entry so its next packet asks again (as a host's neighbour probing would).
A gratuitous ARP updates the entries other interfaces already hold.
"""
from __future__ import annotations

from ..constants import ARP_AGE
from .base import NetworkBase


class ArpMixin(NetworkBase):
    def arp_lookup(self, asker: tuple[str, int], addr: tuple[int, int]) -> tuple[str, int] | None:
        """The interface asker's cache says has addr, or None (a miss)."""
        hit = self.arp_cache.get(asker, {}).get(addr)
        if hit is None or hit[1] <= self.clock:
            return None
        return hit[0]

    def arp_store(self, asker: tuple[str, int], addr: tuple[int, int], owner: tuple[str, int]) -> None:
        self.arp_cache.setdefault(asker, {})[addr] = (owner, self.clock + ARP_AGE)

    def arp_evict(self, asker: tuple[str, int], addr: tuple[int, int]) -> None:
        cache = self.arp_cache.get(asker, {})
        cache.pop(addr, None)
        if not cache:
            self.arp_cache.pop(asker, None)

    def arp_announce(self, owner: tuple[str, int], heard_by) -> None:
        """A gratuitous ARP from owner: interfaces that heard it and already cache its
        address now point there."""
        addr = self.addr_of(owner)  # type: ignore[attr-defined]
        for ifc in heard_by:
            if addr in self.arp_cache.get(ifc, {}):
                self.arp_store(ifc, addr, owner)

    def arp_forget(self, dev_id: str | None = None) -> None:
        """A device's caches empty (it restarted or left); every cache if None."""
        if dev_id is None:
            self.arp_cache = {}
            return
        for ifc in [i for i in self.arp_cache if i[0] == dev_id]:
            del self.arp_cache[ifc]

    def age_arp(self) -> None:
        for ifc, cache in list(self.arp_cache.items()):
            for addr, (_, until) in list(cache.items()):
                if until <= self.clock:
                    self.arp_evict(ifc, addr)
