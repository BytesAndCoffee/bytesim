"""HA router clusters: coordination over control links, virtual gateways, split-brain."""
from __future__ import annotations

from collections import deque

from .base import NetworkBase
from .types import Link


class HAMixin(NetworkBase):
    def cluster_of(self, dev_id: str) -> str | None:
        if not self.is_router(dev_id):
            return None
        return next((g for g in reversed(self.chain(dev_id)) if g in self.clusters), None)

    def ha_state(self, name: str) -> dict:
        """Who is alive, who can coordinate, who is active, and who owns each virtual gateway.

        Members coordinate only over control links. Each control-connected partition
        elects its own active member per network; more than one active partition is
        split-brain. The policy decides which partitions may stay active:
        none (all), quorum (strict majority), witness (majority; a tie goes to the
        partition holding the first surviving member)."""
        members = [m for m in self.devices_in(name) if self.is_router(m)]
        alive = [m for m in members if self.devices[m].up]
        ctrl = [l for l in self.links if l.control and l.a in members and l.b in members]
        ctrl_up = [l for l in ctrl if l.up and self.devices[l.a].up and self.devices[l.b].up]

        def parts(links: list[Link]) -> list[list[str]]:
            out, seen = [], set()
            for m in alive:
                if m in seen:
                    continue
                comp, q = [m], deque([m])
                seen.add(m)
                while q:
                    u = q.popleft()
                    for l in links:
                        if l.touches(u) and (v := l.other(u)[0]) not in seen:
                            seen.add(v)
                            comp.append(v)
                            q.append(v)
                out.append(sorted(comp, key=members.index))
            return out

        partitions = parts(ctrl_up)
        n, policy = len(members), self.clusters.get(name, "none")

        def may_lead(part: list[str]) -> bool:
            if policy == "quorum":
                return 2 * len(part) > n
            if policy == "witness":
                # The witness sees who is alive and breaks ties for the first survivor.
                return 2 * len(part) > n or (2 * len(part) == n and alive[0] in part)
            return True

        active = [p for p in partitions if may_lead(p)]
        owners: dict[int, list[tuple[str, int]]] = {}
        for part in active:
            claimed: set[int] = set()
            for m in part:
                for port, ifc in self.addressed(m):
                    li = self.link_index(m, port)
                    if li is not None and self.usable(self.links[li]) and ifc.net not in claimed:
                        claimed.add(ifc.net)
                        owners.setdefault(ifc.net, []).append((m, port))
        # Redundant coordination: losing any single control link mustn't split anyone.
        redundant = len(partitions) == 1 and all(
            len(parts([k for k in ctrl_up if k is not l])) == 1 for l in ctrl_up)
        dead = [m for m in members if m not in alive]
        leaders = [p[0] for p in active]
        if not alive:
            level, text = "down", "DOWN · every member has failed"
        elif len(active) > 1:
            level, text = "split", ("SPLIT-BRAIN · " + " | ".join(p[0] for p in active)
                                    + " all active, no control link between them")
        elif not active:
            level, text = "down", "OUTAGE · no partition has quorum, all members stood down"
        elif len(partitions) > 1:
            standby = [m for p in partitions if p not in active for m in p]
            level, text = "degraded", f"partitioned · {leaders[0]} active, {' '.join(standby)} stood down ({policy})"
        elif dead:
            level, text = "degraded", f"failover · {' '.join(dead)} down, {leaders[0]} active"
        elif n > 1 and not redundant:
            level, text = "degraded", f"no redundancy · control {len(ctrl_up)}/{len(ctrl)} up, one more failure splits it"
        else:
            level, text = "ok", f"healthy · {leaders[0]} active, control {len(ctrl_up)}/{len(ctrl)} up"
        return {"members": members, "alive": alive, "partitions": partitions, "active": active,
                "owners": owners, "ctrl": len(ctrl), "ctrl_up": len(ctrl_up),
                "policy": policy, "level": level, "text": text}

    def vip_owners(self, net: int) -> list[tuple[str, int]]:
        return [o for c in self.clusters for o in self.ha_state(c)["owners"].get(net, [])]
