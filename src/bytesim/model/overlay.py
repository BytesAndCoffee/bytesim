"""Overlays: a mesh VPN on top of the cabled network, modeled on Tailscale (not a
fully faithful implementation of it).

An overlay is a named list of members (computers, routers, gateways), separate
from groups since it usually spans sites. Each member gets an overlay address
on the overlay's own network (100, 101, …) that doesn't depend on cabling and
stays its own while it's a member. Traffic to an overlay address travels as an
encrypted WireGuard packet between the two members' real interfaces, over
whatever path the underlay offers, and NAT traversal (hole punching) lets it
through NAT gateways: both peers send first, so each NAT sees an outbound flow
before the inbound one.

An overlay's state is a dict: "net" (its network id), "members" (in join order),
"hosts" (member -> overlay host number), and optionally "coord", "exit", "lan",
"routes" and "acl" (see the methods below).
"""
from __future__ import annotations

from typing import NamedTuple

from ..constants import GROUP_NAME, HOST_MAX, REF, OVERLAY_NETS
from .base import NetworkBase
from .types import Leg, NetError, fmt

# words the overlay command reads as subcommands or values, so no overlay can be named them
OVERLAY_RESERVED = ("off", "remove", "advertise", "unadvertise", "peers", "all", "none", "internet")
OVERLAY_KEYS = ("net", "members", "hosts", "coord", "exit", "lan", "routes", "acl")


class Connection(NamedTuple):
    """How a WireGuard packet gets between two members."""
    mode: str                 # "direct" or "relay"
    legs: list[Leg]
    notes: list[str]
    relay: str | None         # the relay used, if any


class OverlayMixin(NetworkBase):
    PEER_TABLE_MAX = 10       # members up to which status shows every pair (it costs a route per pair)

    # ---- membership and overlay addresses --------------------------------------
    def _physical_nets(self) -> set[int]:
        """Network ids interfaces use (an older save, or a static address, can sit in the overlay range)."""
        return {i.net for i in self.ifaces.values() if i.net is not None}  # type: ignore[attr-defined]

    def new_overlay_net(self) -> int:
        used = {t["net"] for t in self.overlays.values()} | self._physical_nets()
        free = [n for n in OVERLAY_NETS if n not in used]
        if not free:
            raise NetError(f"no free overlay network ({OVERLAY_NETS.start}–{OVERLAY_NETS.stop - 1} are all in use)")
        return free[0]

    def new_overlay(self, ov: str, net_id: int | None = None) -> dict:
        """Create an empty overlay (on net_id, or the first free overlay network)."""
        if net_id is not None:
            if net_id not in OVERLAY_NETS:
                raise NetError(f"an overlay's network is {OVERLAY_NETS.start}–{OVERLAY_NETS.stop - 1}")
            if net_id in {t["net"] for t in self.overlays.values()}:
                raise NetError(f"network {net_id} is another overlay's")
            if net_id in self._physical_nets():
                raise NetError(f"network {net_id} is in use by interfaces: pick another overlay network")
        self.overlays[ov] = {"net": net_id if net_id is not None else self.new_overlay_net(),
                             "members": [], "hosts": {}}
        return self.overlays[ov]

    def overlay_members(self, ov: str) -> list[str]:
        """Members in the order they joined."""
        return [d for d in self.overlays[ov]["members"] if d in self.devices]

    def _hosts(self, ov: str) -> dict[str, int]:
        """Each member's overlay host number. A member without one (an older save, or a
        overlay built by hand) gets the lowest free number, in join order."""
        t = self.overlays[ov]
        hosts = t.setdefault("hosts", {})
        for d in t["members"]:
            if d not in hosts:
                hosts[d] = next(h for h in range(1, HOST_MAX) if h not in hosts.values())
        return hosts

    def join_overlay(self, ov: str, dev: str, host: int | None = None) -> None:
        """Add a member, on the given overlay host number or the lowest free one."""
        t, hosts = self.overlays[ov], self._hosts(ov)
        if dev in t["members"]:
            return
        if host is None:
            free = [h for h in range(1, HOST_MAX) if h not in hosts.values()]
            if not free:
                raise NetError(f"overlay {ov} is full ({HOST_MAX - 1} members)")
            host = free[0]
        elif not 1 <= host < HOST_MAX or host in hosts.values():
            raise NetError(f"overlay host {host} is taken or out of range in overlay {ov}")
        t["members"].append(dev)
        hosts[dev] = host

    def leave_overlay(self, ov: str, dev: str) -> list[str]:
        """Remove a member, and the routing roles only a member can hold. Returns notes."""
        t = self.overlays[ov]
        notes = []
        t["members"] = [d for d in t["members"] if d != dev]
        t.get("hosts", {}).pop(dev, None)
        if t.get("exit") == dev:
            del t["exit"]
            notes.append(f"no exit node now ({dev} left)")
        if dev in t.get("routes", []):
            t["routes"].remove(dev)
            notes.append(f"{dev}'s subnet routes went with it")
            if not t["routes"]:
                del t["routes"]
        return notes

    def ol_addr(self, ov: str, dev_id: str) -> tuple[int, int] | None:
        if dev_id not in self.overlay_members(ov):
            return None
        return self.overlays[ov]["net"], self._hosts(ov)[dev_id]

    def ol_addrs(self, dev_id: str) -> list[tuple[str, tuple[int, int]]]:
        """Every (overlay, overlay address) a device has."""
        return [(ov, a) for ov in sorted(self.overlays) if (a := self.ol_addr(ov, dev_id))]

    def ol_resolve(self, addr: tuple[int, int]) -> tuple[str, str] | None:
        """(overlay, member) that owns an overlay address."""
        for ov, t in self.overlays.items():
            if addr[0] == t["net"]:
                hosts = self._hosts(ov)
                return next(((ov, d) for d in self.overlay_members(ov) if hosts[d] == addr[1]), None)
        return None

    def overlays_of(self, dev: str) -> list[str]:
        return [ov for ov in sorted(self.overlays) if dev in self.overlay_members(ov)]

    def overlay_state(self) -> dict[str, dict]:
        """The overlays as saved: fixed key order, every member with its host number."""
        out = {}
        for ov, t in self.overlays.items():
            self._hosts(ov)
            out[ov] = {k: t[k] for k in OVERLAY_KEYS if k in t}
        return out

    def load_overlays(self, data: dict) -> None:
        """Overlays from a save, keeping only what still makes sense (hand edits included)."""
        names = set(self.devices) | set(self.groups) | {"*", "internet"}  # type: ignore[attr-defined]
        self.overlays = {}
        for ov, v in data.items():
            ov = str(ov).lower()                          # commands read names case-insensitively
            if (not isinstance(v, dict) or not GROUP_NAME.match(ov) or REF.match(ov)
                    or ov in OVERLAY_RESERVED or ov in self.overlays):
                continue
            net_id = v.get("net")
            if (net_id not in OVERLAY_NETS or net_id in {t["net"] for t in self.overlays.values()}
                    or net_id in self._physical_nets()):
                net_id = None
            try:
                t = self.new_overlay(ov, net_id)
            except NetError:                              # no free overlay network: drop it, keep the rest
                continue
            hosts = v.get("hosts", {})
            for d in v.get("members", []):
                if d in self.devices and self.devices[d].kind != "s":
                    h = hosts.get(d)
                    self.join_overlay(ov, d, h if isinstance(h, int) and 1 <= h < HOST_MAX
                                      and h not in t["hosts"].values() else None)
            for key in ("coord", "exit"):
                if v.get(key) in self.devices:
                    t[key] = v[key]
            if t.get("exit") not in t["members"]:
                t.pop("exit", None)
            if v.get("lan"):
                t["lan"] = True
            if routes := [d for d in v.get("routes", []) if d in t["members"]]:
                t["routes"] = routes
            if "acl" in v:
                t["acl"] = [list(r) for r in v["acl"] if len(r) == 2 and set(r) <= names]

    # ---- the coordination server -------------------------------------------------
    def coord_state(self, ov: str | None, coord: str | None = None) -> str | None:
        """Why the overlay's coordination server (or ``coord``) can't be used right now (None: fine or none set)."""
        coord = coord or (self.overlays[ov].get("coord") if ov is not None else None)
        if coord is None:
            return None
        if coord not in self.devices:
            return f"coordination server {coord} is gone"
        if not self.devices[coord].up:
            return f"coordination server {coord} is down"
        return None

    def can_register(self, ov: str, dev: str, coord: str | None = None) -> str | None:
        """Why dev can't join the overlay now (None if it can): it must reach the coordination
        server (``coord``, for one about to be set, or the overlay's own)."""
        if (problem := self.coord_state(ov if ov in self.overlays else None, coord)) is not None:
            return problem
        coord = coord or (self.overlays[ov].get("coord") if ov in self.overlays else None)
        if coord and coord != dev:
            try:
                self._underlay(dev, coord)
            except NetError as e:
                return f"{dev} can't reach coordination server {coord} ({e})"
        return None

    # ---- how members connect ---------------------------------------------------
    def _underlay(self, src: str, dst: str, cache: dict | None = None) -> list[Leg]:
        """The shortest underlay route from src to any of dst's addressed interfaces.

        ``cache`` reuses results across the many pairs one read-only query asks about
        (status tables); the network can't change in the meantime."""
        if cache is not None:
            if (src, dst) not in cache:
                try:
                    cache[src, dst] = self._underlay(src, dst)
                except NetError as e:
                    cache[src, dst] = e
            hit = cache[src, dst]
            if isinstance(hit, NetError):
                raise hit
            return hit
        best, err = None, None
        for e in [(dst, p) for p, _ in self.addressed(dst)]:  # type: ignore[attr-defined]
            try:
                # Through the holes the peers punched: NAT lets the tunnel in both ways.
                legs = self.route(src, e, nat=False)  # type: ignore[attr-defined]
            except NetError as x:
                err = x
                continue
            if best is None or sum(len(p) for _, _, p in legs) < sum(len(p) for _, _, p in best):
                best = legs
        if best is None:
            raise NetError(str(err) if err else f"{dst} has no real address")
        return best

    def connection(self, src: str, dst: str, ov: str, acl: bool = True,
                   cache: dict | None = None) -> Connection:
        """How a WireGuard packet gets from src to dst.

        Direct needs hole punching, which works unless both ends are behind hard NAT
        (one that picks a new public port per destination, so the other side can't
        guess it). Otherwise the packet goes through the closest reachable relay:
        both peers keep an outbound connection to it, so no NAT stands in the way."""
        members = self.overlay_members(ov)
        for d in (src, dst):
            if d not in members:
                raise NetError(f"{d} is not in overlay {ov}")
            if not self.devices[d].up:
                raise NetError(f"{d} is down")
        if src == dst:
            raise NetError(f"{fmt(self.ol_addr(ov, src))} is {src}'s own overlay address (loopback)")
        if acl and not self.acl_allows(ov, src, dst):
            raise NetError(f"overlay {ov}'s access rules don't let {src} reach {dst} (acl {ov} allow …)")
        if not self.addressed(dst):  # type: ignore[attr-defined]
            raise NetError(f"{dst} has no real address, so overlay {ov} has no endpoint for it")
        head = f"overlay {ov}: {fmt(self.ol_addr(ov, src))} {src} ⇢ {fmt(self.ol_addr(ov, dst))} {dst}"
        try:
            legs = self._underlay(src, dst, cache)
            out, into = self.nat_crossings(legs)  # type: ignore[attr-defined]
            hard_out = [g for g in out if g in self.nat_hard]
            hard_in = [g for g in into if g in self.nat_hard]
            if hard_out and hard_in:
                direct_err = (f"both ends are behind hard NAT ({', '.join(hard_out)} and {', '.join(hard_in)}): "
                              "hole punching can't guess the ports")
            else:
                notes = [f"{head}, WireGuard from {fmt(self.addr_of(legs[0][0]))} to "  # type: ignore[attr-defined]
                         f"{fmt(self.addr_of(legs[-1][1]))}, direct"]  # type: ignore[attr-defined]
                notes += self.nat_notes(legs)  # type: ignore[attr-defined]
                notes += [f"hole punched through {r}'s NAT: {dst} sent toward {src} first, so {r} treats "
                          f"this as a reply" for r in into]
                return Connection("direct", legs, notes, None)
        except NetError as e:
            direct_err = f"no direct path ({e})"
        options = []
        for relay in self.relays:
            if relay in (src, dst) or not self.devices[relay].up:
                continue
            try:
                first, second = self._underlay(src, relay, cache), self._underlay(relay, dst, cache)
            except NetError:
                continue
            options.append((sum(len(p) for _, _, p in first + second), self.relays.index(relay), relay, first, second))
        if not options:
            raise NetError(f"{head}: {direct_err}, and no relay is reachable by both"
                           + ("" if self.relays else " (none set up: relay <device>)"))
        _, _, relay, first, second = min(options, key=lambda r: r[:2])
        notes = [f"{head}, relayed via {relay}: {direct_err}",
                 f"  {src} → {relay} on {src}'s outbound connection; {relay} → {dst} on {dst}'s"]
        notes += self.nat_notes(first)  # type: ignore[attr-defined]
        return Connection("relay", first + second, notes, relay)

    def tunnel(self, src: str, dst: str, ov: str) -> tuple[list[Leg], list[str]]:
        """The underlay legs a WireGuard packet from src to dst takes, and what to say about it."""
        c = self.connection(src, dst, ov)
        return c.legs, c.notes

    def nat_situation(self, dev: str) -> str:
        """Where a member stands: "public", "easy NAT g3", "hard NAT g4", or why it's offline."""
        if not self.devices[dev].up:
            return "down"
        try:
            legs = self.route(dev, None)  # type: ignore[attr-defined]
        except NetError:
            return "no internet"
        out, _ = self.nat_crossings(legs)  # type: ignore[attr-defined]
        if not out:
            return "public"
        hard = [g for g in out if g in self.nat_hard]
        return f"hard NAT {' '.join(hard)}" if hard else f"easy NAT {' '.join(out)}"

    def peer_table(self, ov: str, of: list[str] | None = None) -> dict[tuple[str, str], str]:
        """How member pairs connect: "direct", "relay <device>", "denied" or "✗ <why>".
        ``of`` limits it to those members' connections (a route per pair, so keep it small)."""
        out: dict[tuple[str, str], str] = {}
        cache: dict = {}
        members = [d for d in self.overlay_members(ov) if self.devices[d].up]
        with self.remembering():  # type: ignore[attr-defined]
            for a in (of if of is not None else members):
                for b in members:
                    if a == b:
                        continue
                    if not self.acl_allows(ov, a, b):
                        out[a, b] = "denied"
                        continue
                    try:
                        c = self.connection(a, b, ov, cache=cache)
                    except NetError as e:
                        out[a, b] = f"✗ {e}"
                        continue
                    out[a, b] = "direct" if c.mode == "direct" else f"relay {c.relay}"
        return out

    # ---- access rules -------------------------------------------------------------
    def _acl_match(self, sel: str, target: str) -> bool:
        if sel == "*":                             # keywords first, even if a group shares the name
            return target != "internet"            # the internet must be named explicitly
        if sel == "internet":
            return target == "internet"
        if sel in self.groups:  # type: ignore[attr-defined]
            return target in self.devices_in(sel)  # type: ignore[attr-defined]
        return sel == target

    def acl_allows(self, ov: str, src: str, target: str) -> bool:
        """May src start traffic to target (a device id, or "internet")? Without access rules,
        everything; with them (even an empty list), only what a rule allows."""
        if "acl" not in self.overlays[ov]:
            return True
        return any(self._acl_match(a, src) and self._acl_match(b, target) for a, b in self.overlays[ov]["acl"])

    # ---- subnet routers and exit nodes ------------------------------------------
    def _nets(self, dev: str) -> set[int]:
        return {i.net for _, i in self.addressed(dev)}  # type: ignore[attr-defined]

    def _stays_inside(self, src: str, dst: tuple[str, int]) -> bool:
        """Can src reach dst without leaving through a NAT gateway (within its own site)?"""
        try:
            legs = self.route(src, dst)  # type: ignore[attr-defined]
        except NetError:
            return False
        return not self.nat_crossings(legs)[0]  # type: ignore[attr-defined]

    def overlay_via(self, src: str, dst: tuple[str, int] | None) -> tuple[str, str, str] | None:
        """(overlay, member, "subnet" | "exit") that should carry src's traffic to dst, if any.

        A subnet router advertises its own LAN networks: members reach devices there
        through it. An exit node carries everything not on src's own networks, the
        internet included; with ``lan`` on, it leaves alone what src reaches without
        leaving its site (LAN access)."""
        local = self._nets(src)
        dnet = self.addr_of(dst)[0] if dst is not None else None  # type: ignore[attr-defined]
        if dnet is not None and dnet in local:
            return None
        for ov in self.overlays_of(src):
            t, members = self.overlays[ov], self.overlay_members(ov)
            if dnet is not None:
                for r in t.get("routes", []):
                    if r != src and r in members and self.devices[r].up and dnet in self._nets(r):
                        return ov, r, "subnet"
            ex = t.get("exit")
            if ex and ex != src and ex in members:
                if dst is not None and t.get("lan") and self._stays_inside(src, dst):
                    continue
                return ov, ex, "exit"
        return None

    def routed_via(self, src: str, dst: tuple[str, int] | None, ov: str, via: str,
                   kind: str) -> tuple[list[Leg], list[str]]:
        """src → via through the overlay, then via forwards to dst (or the internet) itself."""
        what = f"{kind} {'router' if kind == 'subnet' else 'node'} {via}"
        target = dst[0] if dst is not None else "internet"
        if not self.acl_allows(ov, src, target):
            raise NetError(f"overlay {ov}'s access rules don't let {src} reach {target} through {what}")
        if not self.devices[via].up:
            raise NetError(f"{what} is down")
        c = self.connection(src, via, ov, acl=False)
        first, notes = c.legs, c.notes
        try:
            second = self.route(via, dst)  # type: ignore[attr-defined]
            blocked = None
        except NetError:
            if dst is None or (blocked := self.nat_drop(via, dst)) is None:  # type: ignore[attr-defined]
                raise
        if blocked is not None:
            if kind == "exit" and dst is not None and self._stays_inside(src, dst):
                gw = blocked.split("'")[0].removeprefix("dropped by ")
                raise NetError(f"{target} is in {src}'s own site, but with exit node {via} that traffic "
                               f"goes out through it and {gw}'s NAT won't let it back in; "
                               f"overlay {ov} lan=on keeps it local")
            raise NetError(f"after {what}: {blocked}")
        out = second[0][0] if second else None
        if out is None:                           # the destination is the router's own LAN address
            notes.append(f"{what} answers on its own address"
                         + (f" {fmt(self.addr_of(dst))}" if dst else ""))  # type: ignore[attr-defined]
        elif kind == "subnet":
            notes.append(f"subnet router {via} forwards onto its LAN as {fmt(self.addr_of(out))} "  # type: ignore[attr-defined]
                         f"(source NAT), so {target} needn't know the overlay")
        else:
            notes.append(f"exit node {via}: traffic leaves through it, as {fmt(self.addr_of(out))}")  # type: ignore[attr-defined]
        notes += self.nat_notes(second)  # type: ignore[attr-defined]
        return first + second, notes
