"""Synthesize a ByteSim command script that rebuilds a network.

The script is written in ByteSim's own console language, so it can be replayed
with ``run <file>`` (after ``clear``). Every script is checked before it is
returned: it is replayed into a fresh network by the real command handlers
(``bytesim.scripting``) and compared with the original (``model.equivalence``).

How it works:

1. **Explicit plan.** Devices are created in id order (``add s 5 3`` makes the
   next three switches), with placeholders created and deleted to recreate gaps
   in the numbering. Then groups (innermost first), HA clusters, interface
   settings, every cable on its exact ports, STP and DHCP-server settings, and
   finally failures. This always works, and it is the fallback.
2. **Reuse.** ByteSim's ``clone`` is deterministic: the source's devices, sorted
   by kind and number, get the next free ids in order. So a group B can be
   cloned from an earlier group A exactly when that one mapping preserves
   kinds, port counts, internal cables (with ports and roles), the nested group
   tree, clusters and settings, and B's ids are consecutive. ``ports copy`` and
   ``copy A(…) B(…)`` reproduce a group's cabling to other groups when it follows
   another group's pattern, with groups matched by position.
3. **Choice.** Candidates are tried best saving first; each is kept only if the
   script with it still replays into an equivalent network and is shorter.
   Search is bounded (``Limits``), so ``shortest`` means the shortest script
   found within those limits, not a proven optimum.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations, permutations

from .constants import KINDS, RANK
from .model import NetError, Network
from .model.equivalence import DEFAULT_MODE, differences, iface_config

MODES = ("explicit", "readable", "shortest")
READABLE_MIN_SAVING = 3        # readable mode only reuses structure when it saves this many commands


@dataclass
class Limits:
    max_candidates: int = 60   # candidate substitutions tried (each costs one replay)
    max_seconds: float = 8.0   # total optimisation time
    max_group_size: int = 300  # larger groups aren't considered for reuse


@dataclass
class Substitution:
    kind: str                                  # clone | copy | ports
    src: str
    dst: str
    pairs: list[tuple[str, str]] = field(default_factory=list)
    claims: Counter = field(default_factory=Counter)   # (dst device, endpoint device, role) cables it makes
    saves: int = 0

    def command(self) -> str:
        if self.kind == "clone":
            return f"clone {self.src} {self.dst}"
        old = ",".join(o for o, _ in self.pairs)
        new = ",".join(n for _, n in self.pairs)
        prefix = "ports copy" if self.kind == "ports" else "copy"
        return f"{prefix} {self.src}({old}) {self.dst}({new})"

    def describe(self) -> str:
        if self.kind == "clone":
            return f"{self.dst} has the same structure as {self.src}: clone it"
        ends = ", ".join(f"{o}→{n}" for o, n in self.pairs)
        if self.kind == "copy":
            return f"{self.dst} is {self.src} again, cabled the same way with {ends}: copy it"
        return f"{self.dst} is cabled like {self.src}, with {ends}"


@dataclass
class Synthesis:
    script: str
    mode: str
    commands: int
    explicit_commands: int
    used: list[Substitution]
    limitations: list[str]
    tried: int = 0
    rejected: int = 0


class _Infeasible(Exception):
    pass


def _order(did: str) -> tuple[int, int]:
    return RANK[did[0]], int(did[1:])


def _commands(lines: list[str]) -> int:
    return sum(1 for ln in lines if ln and not ln.startswith("#"))


def _cost(lines: list[str]) -> tuple[int, int]:
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    return len(body), sum(len(ln) for ln in body)


# ---------------------------------------------------------------------------- structure helpers
def _subtree(net: Network, g: str) -> list[str]:
    out = [g]
    for m in net.groups[g]:
        if m in net.groups:
            out += _subtree(net, m)
    return out


def _postorder(net: Network, g: str) -> list[str]:
    out: list[str] = []
    for m in net.groups[g]:
        if m in net.groups:
            out += _postorder(net, m)
    return out + [g]


def _clone_map(net: Network, a: str, b: str) -> dict[str, str] | None:
    """The device mapping `clone a` would produce if b's ids were the ones allocated."""
    da, db = net.devices_in(a), net.devices_in(b)
    if len(da) != len(db) or set(da) & set(db):
        return None
    phi = {}
    for k in KINDS:
        xa = sorted((d for d in da if d[0] == k), key=_order)
        xb = sorted((d for d in db if d[0] == k), key=_order)
        if len(xa) != len(xb):
            return None
        nums = [int(d[1:]) for d in xb]
        if nums and (nums != list(range(nums[0], nums[0] + len(nums))) or int(xa[-1][1:]) >= nums[0]):
            return None                        # b's ids must be consecutive and come after a's
        phi.update(zip(xa, xb))
    return phi


def _clone_equivalent(net: Network, a: str, b: str, phi: dict[str, str]) -> bool:
    for x, y in phi.items():
        dx, dy = net.devices[x], net.devices[y]
        if dx.nports != dy.nports:
            return False
        if (x in net.stp_off) != (y in net.stp_off) or net.stp_priority.get(x) != net.stp_priority.get(y):
            return False
        if (x in net.dhcp_off) != (y in net.dhcp_off):
            return False
        if dx.kind != "s":
            for p in range(1, dx.nports + 1):
                mode, _ = iface_config(net, x, p)
                if mode == "static":           # clone turns static into dhcp/auto, without addresses
                    mode = "dhcp" if dx.kind == "c" else "auto"
                if iface_config(net, y, p) != (mode, None):
                    return False

    def same_tree(ga: str, gb: str) -> bool:
        ma, mb = net.groups[ga], net.groups[gb]
        if (len(ma) != len(mb) or net.clusters.get(ga) != net.clusters.get(gb)
                or net.cluster_modes.get(ga) != net.cluster_modes.get(gb)):
            return False
        for x, y in zip(ma, mb):
            if x in net.groups:
                if y not in net.groups or not same_tree(x, y):
                    return False
            elif phi.get(x) != y:
                return False
        return True

    if not same_tree(a, b):
        return False
    inside_a, inside_b = set(phi), set(phi.values())
    lags_a = {frozenset(phi[d] for d in p) for p in net.lags if p <= inside_a}
    if lags_a != {p for p in net.lags if p <= inside_b}:
        return False
    from .model.equivalence import link_key
    want = Counter(link_key(phi[l.a], l.pa, phi[l.b], l.pb, l.role)
                   for l in net.links if l.a in inside_a and l.b in inside_a)
    have = Counter(link_key(l.a, l.pa, l.b, l.pb, l.role)
                   for l in net.links if l.a in inside_b and l.b in inside_b)
    return want == have


def _boundary(net: Network, devs: set[str]) -> list[tuple[str, str, str]]:
    """(inside device, outside device, role) for every cable leaving a device set."""
    out = []
    for l in net.links:
        if (l.a in devs) != (l.b in devs):
            near, far = (l.a, l.b) if l.a in devs else (l.b, l.a)
            out.append((near, far, l.role))
    return out


def _endpoint_groups(net: Network, group: str, boundary: list[tuple[str, str, str]]) -> list[str]:
    """Innermost groups of a boundary's far ends, in first-seen order (not ones containing the group)."""
    around = set(net.chain(group)) | {group}
    out: list[str] = []
    for _, far, _ in boundary:
        g = net.group_of(far)
        if g and g not in around and g not in out:
            out.append(g)
    return out


def _pattern(net: Network, a: str, near_map: dict[str, str],
             pairs: list[tuple[str, str]]) -> Counter | None:
    """The cables (dst device, new endpoint device, role) a copy of a's boundary would make."""
    ends: dict[str, str] = {}
    for e, f in pairs:
        de, df = net.devices_in(e), net.devices_in(f)
        if len(de) != len(df):
            return None
        for x, y in zip(de, df):
            if net.devices[x].kind != net.devices[y].kind or x in ends:
                return None
            ends[x] = y
    out: Counter = Counter()
    for near, far, role in _boundary(net, set(near_map)):
        if far in ends:
            out[(near_map[near], ends[far], role)] += 1
    return out


def _best_rewire(net: Network, a: str, b: str, near_map: dict[str, str]) -> tuple[list, Counter] | None:
    """Endpoint pairs under which b's outside cabling repeats a's (largest match wins)."""
    ba, bb = _boundary(net, set(near_map)), _boundary(net, set(near_map.values()))
    ea, fb = _endpoint_groups(net, a, ba), _endpoint_groups(net, b, bb)
    if not ea or not fb or len(ea) > 4 or len(fb) > 4:
        return None
    have = Counter((near, far, role) for near, far, role in bb)
    inside_b = set(near_map.values())
    best = None
    for r in range(min(len(ea), len(fb)), 0, -1):
        for es in combinations(ea, r):
            for fs in permutations(fb, r):
                pairs = list(zip(es, fs))
                if any(set(net.devices_in(f)) & inside_b for f in fs):
                    continue
                pat = _pattern(net, a, near_map, pairs)
                if not pat or pat - have:
                    continue
                if best is None or sum(pat.values()) > sum(best[1].values()):
                    best = (pairs, pat)
        if best:
            return best
    return None


def _explicit_cost(net: Network, g: str) -> int:
    devs = net.devices_in(g)
    inside = set(devs)
    return (len({d[0] for d in devs}) + len(_subtree(net, g))
            + sum(1 for l in net.links if l.a in inside and l.b in inside)
            + sum(1 for d in devs if d in net.stp_priority or d in net.dhcp_off)
            + sum(1 for x in _subtree(net, g) if x in net.clusters))


def candidates(net: Network, limits: Limits) -> list[Substitution]:
    """Every reuse the network allows, best saving first (deterministic order)."""
    out: list[Substitution] = []
    groups = [g for g in net.groups if len(net.devices_in(g)) <= limits.max_group_size]
    for b in groups:
        for a in groups:
            if a == b or a in _subtree(net, b) or b in _subtree(net, a):
                continue
            if set(net.devices_in(a)) & set(net.devices_in(b)):
                continue
            phi = _clone_map(net, a, b)
            if phi is not None and _clone_equivalent(net, a, b, phi):
                saved = _explicit_cost(net, b) - 1
                out.append(Substitution("clone", a, b, saves=saved))
                rw = _best_rewire(net, a, b, phi)
                if rw:
                    out.append(Substitution("copy", a, b, rw[0], rw[1], saved + sum(rw[1].values())))
            da, db = net.devices_in(a), net.devices_in(b)
            if len(da) == len(db) and all(net.devices[x].kind == net.devices[y].kind for x, y in zip(da, db)):
                rw = _best_rewire(net, a, b, dict(zip(da, db)))
                if rw and sum(rw[1].values()) > 1:
                    out.append(Substitution("ports", a, b, rw[0], rw[1], sum(rw[1].values()) - 1))
    rank = {"copy": 0, "clone": 1, "ports": 2}
    out.sort(key=lambda s: (-s.saves, rank[s.kind], list(net.groups).index(s.dst), s.src))
    return out


# ---------------------------------------------------------------------------- rendering
def render(net: Network, subs: list[Substitution], mode: str = "readable") -> tuple[list[str], list[str]]:
    """The script (lines) for a plan, and the properties the DSL can't express.

    Raises _Infeasible if the substitutions can't be ordered into a valid script."""
    comments = mode != "shortest"
    lines: list[str] = []
    limitations: list[str] = []

    def say(text: str) -> None:
        if comments:
            lines.append(f"# {text}")

    def blank() -> None:
        if comments and lines and lines[-1]:
            lines.append("")

    def drop_empty_heading() -> None:
        """Remove a trailing heading (and blank line) that no command followed."""
        while lines and (lines[-1].startswith("#") or not lines[-1]):
            lines.pop()

    clones = sorted((s for s in subs if s.kind in ("clone", "copy")),
                    key=lambda s: min(_order(d) for d in net.devices_in(s.dst)))
    copies = [s for s in subs if s.kind == "ports"]
    produced: dict[str, Substitution] = {d: s for s in clones for d in net.devices_in(s.dst)}
    produced_groups = {g for s in clones for g in _subtree(net, s.dst)}

    # Which cables the substitutions make; the rest are written out.
    made: set[int] = set()
    for i, l in enumerate(net.links):
        sa, sb = produced.get(l.a), produced.get(l.b)
        if sa is not None and sa is sb:
            made.add(i)                                    # inside a clone: copied with its ports
    for s in [c for c in clones if c.kind == "copy"] + copies:
        for (x, y, role), n in s.claims.items():
            for i, l in enumerate(net.links):
                if n and i not in made and l.role == role and {l.a, l.b} == {x, y}:
                    made.add(i)
                    n -= 1
            if n:
                raise _Infeasible(f"{s.command()} claims cables that aren't there")

    present = {k: {d.num: d for d in net.devices.values() if d.kind == k} for k in KINDS}
    top = {k: max([net.counters[k], *present[k]]) for k in KINDS}
    nxt = {k: 1 for k in KINDS}
    created: set[str] = set()
    done_groups: set[str] = set(produced_groups)
    done_links: set[int] = set(made)
    configured: set[str] = set(produced)                    # clones copy settings
    bundled: set[frozenset] = {p for p in net.lags if any(p <= set(net.devices_in(c.dst)) for c in clones)}

    def lag_cmds(devs: set[str]) -> list[str]:
        out = []
        for pair in sorted(net.lags, key=lambda q: sorted(q, key=_order)):
            if pair <= devs and pair not in bundled:
                bundled.add(pair)
                out.append(f"lag {' '.join(sorted(pair, key=_order))}")
        return out

    # ---- devices, in id order (gaps recreated with placeholders) ----------
    def create(k: str, upto: int) -> None:
        n = nxt[k]
        while n <= upto:
            if f"{k}{n}" in produced:
                raise _Infeasible("a clone's ids would be taken by explicit devices")
            m = n
            if n in present[k]:
                ports = present[k][n].nports
                while (m + 1 <= upto and m + 1 in present[k] and present[k][m + 1].nports == ports
                       and m + 1 - n < 100 and f"{k}{m + 1}" not in produced):
                    m += 1
                lines.append(f"add {k} {ports} {m - n + 1}")
                created.update(f"{k}{i}" for i in range(n, m + 1))
            else:
                while m + 1 <= upto and m + 1 not in present[k] and m + 1 - n < 100:
                    m += 1
                ids = [f"{k}{i}" for i in range(n, m + 1)]
                say(f"{' '.join(ids)} {'was' if len(ids) == 1 else 'were'} deleted: "
                    f"create and remove placeholders so later ids match")
                lines.append(f"add {k} 1 {len(ids)}")
                lines.append("del " + " ".join(ids))
            n = m + 1
        nxt[k] = max(nxt[k], upto + 1)

    # ---- per-device settings ------------------------------------------------
    def ip_commands(d: str) -> list[str]:
        dev = net.devices[d]
        out = []
        if dev.kind == "s":
            return out
        for p in range(1, dev.nports + 1):
            mode_, static = iface_config(net, d, p)
            if (mode_, static) == (DEFAULT_MODE[dev.kind], None):
                continue
            where = f"{d}:{p}"
            if mode_ == "static" and static:
                n_, h, gw = static
                out.append(f"ip {where} ({n_},{h})" + (f" {gw}" if gw is not None and dev.kind == "c" else ""))
            elif mode_ in ("none", "adhoc") and (mode_ == "none" or dev.kind == "c"):
                out.append(f"ip {where} {mode_}")
            elif mode_ == "auto" and dev.kind != "c":
                continue
            else:
                limitations.append(f"interface {where} mode {mode_!r} can't be set from the console")
        return out

    def settings(devs: list[str], stp_now: bool) -> tuple[list[str], list[str]]:
        """(commands, switches to turn STP off for) for devices not yet configured."""
        cmds, off = [], []
        for d in sorted(devs, key=_order):
            if d in configured:
                continue
            configured.add(d)
            cmds += ip_commands(d)
            if d in net.stp_priority:
                cmds.append(f"stp priority {d} {net.stp_priority[d]}")
            if d in net.dhcp_off:
                cmds.append(f"dhcpd {d} off")
            if d in net.stp_off:
                off.append(d)
        if stp_now and off:
            cmds.append(f"stp {' '.join(off)} off")
            off = []
        return cmds, off

    def group_cmds(roots: list[str]) -> list[str]:
        out = []
        for root in roots:
            for g in _postorder(net, root):
                if g in done_groups:
                    continue
                done_groups.add(g)
                out.append(f"group {g} {' '.join(net.groups[g])}")
                if g in net.clusters:
                    router = next((m for m in net.groups[g] if m in net.devices and net.is_router(m)), None)
                    if router:
                        mode = f" mode={net.cluster_modes[g]}" if g in net.cluster_modes else ""
                        out.append(f"cluster {g} {router} policy={net.clusters[g]}{mode}")
                    else:
                        limitations.append(f"cluster {g} has no router directly in it, so `cluster` "
                                           f"can't mark it (policy {net.clusters[g]})")
        return out

    def cable(i: int) -> str:
        l = net.links[i]
        return f"{l.a}:{l.pa} {l.b}:{l.pb}" + (f" role={l.role}" if l.role != "data" else "")

    def cables_for(devs: set[str], others: set[str] | None = None) -> list[str]:
        out = []
        for i, l in enumerate(net.links):
            if i in done_links:
                continue
            inside = l.a in devs and l.b in devs
            crossing = others is not None and ((l.a in devs and l.b in others) or (l.b in devs and l.a in others))
            if inside or crossing:
                done_links.add(i)
                out.append(cable(i))
        return out

    # ---- the plan ------------------------------------------------------------
    pending = list(clones)
    phase = 0
    while True:
        limit = {k: min([min(int(d[1:]) for d in net.devices_in(c.dst) if d[0] == k)
                         for c in pending if any(d[0] == k for d in net.devices_in(c.dst))],
                        default=top[k] + 1) - 1 for k in KINDS}
        before = len(lines)
        if any(nxt[k] <= limit[k] for k in KINDS):
            blank()
            say("Devices: add <kind> <ports> <count> creates the next ids of that kind" if phase == 0
                else "More devices, up to the next reused group's ids")
        for k in KINDS:
            create(k, limit[k])
        phase += 1 if len(lines) > before else 0
        if not pending:
            break
        c = pending.pop(0)
        dst = net.devices_in(c.dst)
        for k in KINDS:
            ids = sorted((int(d[1:]) for d in dst if d[0] == k))
            if ids and nxt[k] != ids[0]:
                raise _Infeasible(f"{c.dst}'s {KINDS[k][0]} ids don't follow on")
        src = net.devices_in(c.src)
        if not set(src) <= created:
            raise _Infeasible(f"{c.src} isn't built before {c.dst}")
        # Finish the source (groups, internal cables, settings) and, for copy, its endpoints.
        roots = [c.src] + [g for pair in c.pairs for g in pair]
        need = {d for g in roots for d in net.devices_in(g)}
        if not need <= created:
            raise _Infeasible(f"endpoints of {c.command()} aren't built yet")
        blank()
        say(f"Finish {c.src}" + (" and the groups its copy connects to" if c.pairs else "")
            + " so it can be reused")
        mark = len(lines)
        lines.extend(group_cmds(roots))
        cmds, _ = settings(src, stp_now=True)
        lines.extend(cmds)
        ends = {d for _, f in c.pairs for d in net.devices_in(f)} | {d for e, _ in c.pairs for d in net.devices_in(e)}
        lines.extend(cables_for(set(src), ends if c.kind == "copy" else None))
        lines.extend(lag_cmds(set(src)))           # before the clone, so the clone copies them
        if len(lines) == mark:
            drop_empty_heading()
        blank()
        say(c.describe())
        lines.append(c.command())
        created.update(dst)
        for k in KINDS:
            ids = [int(d[1:]) for d in dst if d[0] == k]
            if ids:
                nxt[k] = max(ids) + 1

    # ---- everything else ------------------------------------------------------
    roots = [g for g in net.groups if net.parent_of(g) is None]
    gcmds = group_cmds(roots)
    if gcmds:
        blank()
        say("Groups, innermost first (group <name> <members…>)")
        lines.extend(gcmds)
    cmds, off = settings([d for d in net.devices if d not in configured], stp_now=False)
    ipc = [c for c in cmds if c.startswith("ip ")]
    other = [c for c in cmds if not c.startswith("ip ")]
    if ipc:
        blank()
        say("Interface settings (computers use DHCP and router ports set themselves up otherwise)")
        lines.append("mode addr")
        lines.extend(ipc)
    rest = [i for i in range(len(net.links)) if i not in done_links]
    if rest:
        blank()
        say("Cables, on their exact ports: <device>:<port> <device>:<port> [role=…]")
        if mode == "readable":                   # by the top-level group they belong to
            top_of = {d: (net.chain(d) or [None])[0] for d in net.devices}
            order = [g for g in net.groups if net.parent_of(g) is None]
            buckets: dict[str | None, list[int]] = {}
            for i in rest:
                l = net.links[i]
                key = top_of[l.a] if top_of[l.a] == top_of[l.b] else None
                buckets.setdefault(key, []).append(i)
            for key in [g for g in order if g in buckets] + ([None] if None in buckets else []):
                if len(buckets) > 1:
                    say(f"inside {key}" if key else "between groups")
                lines.extend(cable(i) for i in buckets[key])
        else:
            lines.extend(cable(i) for i in rest)
        done_links.update(rest)
    for s in copies:
        blank()
        say(s.describe())
        lines.append(s.command())
    remaining_lags = lag_cmds(set(net.devices))
    if remaining_lags:
        blank()
        say("Link aggregations: parallel cables that act as one link")
        lines.extend(remaining_lags)
    if other or off:
        blank()
        say("Switch and router settings")
        lines.extend(other)
        if off:
            lines.append(f"stp {' '.join(off)} off")
    down_links = [i for i, l in enumerate(net.links) if not l.up]
    down_devs = [d for d in net.sorted_devices() if not d.up]
    if down_links or down_devs:
        blank()
        say("Runtime state: failures")
        lines.extend(f"fail {net.links[i].a}:{net.links[i].pa}" for i in down_links)
        lines.extend(f"fail {d.id}" for d in down_devs)
    return lines, sorted(set(limitations))


# ---------------------------------------------------------------------------- verification
def _ignore(limitations: list[str]) -> set[str]:
    """Canonical keys the DSL can't express (masked out of the comparison, and reported)."""
    out = set()
    for text in limitations:
        if text.startswith("cluster "):
            out.add(f"clusters {text.split()[1]}")
        elif text.startswith("interface "):
            out.add(f"ifaces {text.split()[1]}")
    return out


def verify(net: Network, script: str, limitations: list[str] | None = None) -> list[str]:
    """Replay a script into a fresh network and compare. [] means equivalent."""
    from .scripting import replay
    try:
        got = replay(script)
    except NetError as e:
        return [f"replay failed: {e}"]
    masked = _ignore(limitations or [])
    return [d for d in differences(net, got, limit=1000)
            if not any(d.startswith(m + ":") for m in masked)][:8]


def _header(net: Network, mode: str, body: list[str], explicit: int, used: list[Substitution],
            limitations: list[str]) -> list[str]:
    n = _commands(body)
    out = [f"# ByteSim construction script ({mode}) — {len(net.devices)} devices, "
           f"{len(net.links)} cables, {len(net.groups)} groups in {n} commands"]
    if mode != "shortest":
        out += ["# Replay it into an empty network: `clear`, then `run <this file>`.",
                "# Rebuilds: device ids and port counts, groups, cables (exact ports) and roles,",
                "#   HA clusters, STP / DHCP-server / interface settings, failures.",
                "# Not rebuilt: positions (auto-arranged), learned addresses (re-learned),",
                "#   MAC tables, storms."]
        if used:
            out.append(f"# Reuses structure {len(used)} time{'s' if len(used) != 1 else ''}: "
                       f"{explicit} commands without it.")
    for text in limitations:
        out.append(f"# NOT EXPRESSIBLE: {text}")
    out.append("# Verified: replaying this script rebuilds an equivalent network.")
    return out


# ---------------------------------------------------------------------------- the entry point
def synthesize(net: Network, mode: str = "readable", limits: Limits | None = None) -> Synthesis:
    """A verified construction script for ``net``. Raises NetError if none can be made."""
    if mode not in MODES:
        raise NetError(f"mode must be one of: {', '.join(MODES)}")
    limits = limits or Limits()
    body, limitations = render(net, [], mode)
    problems = verify(net, "\n".join(body), limitations)
    if problems:
        raise NetError("couldn't build a script that rebuilds this network: " + "; ".join(problems))
    explicit = _commands(body)
    used: list[Substitution] = []
    tried = rejected = 0
    if mode != "explicit":
        floor = READABLE_MIN_SAVING if mode == "readable" else 1
        start = time.monotonic()
        for cand in candidates(net, limits):
            if tried >= limits.max_candidates or time.monotonic() - start > limits.max_seconds:
                break
            if cand.saves < floor or _conflicts(net, used, cand):
                continue
            if mode == "readable" and _first(net, cand.dst) < _first(net, cand.src):
                continue                       # readable: later groups follow earlier ones, not back
            tried += 1
            try:
                trial, lim = render(net, used + [cand], mode)
            except _Infeasible:
                rejected += 1
                continue
            if _cost(trial) >= _cost(body) or verify(net, "\n".join(trial), lim):
                rejected += 1
                continue
            used.append(cand)
            body, limitations = trial, lim
    lines = _header(net, mode, body, explicit, used, limitations) + ([""] if mode != "shortest" else []) + body
    while lines and not lines[-1]:
        lines.pop()
    return Synthesis("\n".join(lines) + "\n", mode, _commands(body), explicit, used, limitations,
                     tried, rejected)


def _first(net: Network, g: str) -> tuple[int, int]:
    return min(_order(d) for d in net.devices_in(g))


def _conflicts(net: Network, used: list[Substitution], cand: Substitution) -> bool:
    """Two reuses can't both make the same devices or the same cables."""
    def devs(s: Substitution) -> set[str]:
        return set(net.devices_in(s.dst)) if s.kind in ("clone", "copy") else set()

    mine = devs(cand)
    claimed: Counter = Counter()
    for s in used:
        if mine & devs(s) or cand.dst == s.dst and cand.kind != "ports" and s.kind != "ports":
            return True
        if cand.kind in ("clone", "copy") and s.kind in ("clone", "copy") and (
                cand.dst in _subtree(net, s.dst) or s.dst in _subtree(net, cand.dst)):
            return True
        claimed += s.claims
    if cand.claims:
        have = Counter()
        for l in net.links:
            have[(l.a, l.b, l.role)] += 1
            have[(l.b, l.a, l.role)] += 1
        return bool((claimed + cand.claims) - have)
    return False
