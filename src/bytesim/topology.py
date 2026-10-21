"""The declarative topology format (``bytesim/topology-v1``): load and export.

A topology file describes the *architecture*: devices, groups and cables,
optionally built from reusable templates and link macros. It leaves out
runtime state: positions (auto-arranged), addresses (re-learned), failures and
storms. Ports are allocated in file order unless pinned (``r1:3``).

    schema: bytesim/topology-v1
    defaults: {links: data, stp: on, interfaces: {computer: dhcp}}
    templates:
      site:
        devices:                     # roles
          routers:  {kind: router, count: 2, ports: 8}
          access:   {kind: switch, count: 2, ports: 5}
        groups:                      # local names; a list = members, a mapping = subgroups
          core: routers
          office: {room1: ["access[0]"], room2: ["access[1]"]}
        links:
          - mesh: [routers, access]
    instances:
      site1:
        template: site
        group: site1                 # wrap the instance in a group (default: its own name)
        devices: {routers: [r1, r2], access: [s1, s2]}   # optional: fixed ids
        groups: {core: rfab1, office.room1: room1}       # optional: names by key or dotted path
    devices: {s8: 5, s12: {ports: 5, stp: off}}           # stand-alone devices: id → ports | spec
    groups: {link1: [s8, s12]}
    links:
      - parallel: [s8, s12, 2]
      - {connect: ["r1:5", "r2:5"], role: control}

Link macros: ``connect: [a, b]``, ``mesh: [A, B]`` (every pairing),
``zip: [A, B]`` (pairwise), ``parallel: [a, b, n]``, ``chain: [a, b, c…]`` and
``ring: [a, b, c…]``. A name can be a device id (optionally ``:port``), a role
or ``role[i]`` inside a template, a group (all devices in it), or a list.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from .constants import ALIASES, AUTO, CLUSTER_MODES, GROUP_NAME, KINDS, POLICIES, RANK, REF, ROLES
from .model import NetError, Network
from .model.macros import macro_pairs
from .model.overlay import OVERLAY_RESERVED
from .model.types import Device, Iface

SCHEMA = "bytesim/topology-v1"
GROUP_KEYS = {"members", "cluster", "mode"}
INDEXED = re.compile(r"^([A-Za-z_][\w-]*)\[(\d+)\](?::(?:p|nic|eth)?(\d+))?$")
KIND_NAME = {name: k for k, (name, _, _) in KINDS.items()}


def looks_like_topology(text: str) -> bool:
    """True for a topology file (YAML, or JSON with a schema key), False for a save file."""
    head = text.lstrip()[:400]
    return "bytesim/topology" in head or (not head.startswith("{") and "counters" not in head)


# ---------------------------------------------------------------------------- loading
class _Builder:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.net = Network()
        d = spec.get("defaults") or {}
        self.default_role = str(d.get("links", "data"))
        self.default_stp = _onoff(d.get("stp", "on"), "defaults.stp")
        self.default_ifaces = {_kind(k, "defaults.interfaces"): str(v)
                               for k, v in (d.get("interfaces") or {}).items()}
        if self.default_role not in ROLES:
            raise NetError(f"defaults.links: role must be one of {', '.join(ROLES)}")

    # ---- devices ---------------------------------------------------------
    def _declare(self, did: str, kind: str, spec: Any, where: str) -> Device:
        m = REF.match(did)
        if not m or m[3]:
            raise NetError(f"{where}: {did!r} isn't a device id like r1 or s12")
        if m[1].lower() != kind:
            raise NetError(f"{where}: {did} is named like a {KINDS[m[1].lower()][0]} but declared a {KINDS[kind][0]}")
        did = did.lower()
        if did in self.net.devices:
            raise NetError(f"{where}: {did} is declared twice")
        spec = {"ports": spec} if isinstance(spec, int) else dict(spec or {})
        dev = Device(kind, int(m[2]), int(spec.get("ports", KINDS[kind][1])))
        if not 1 <= dev.nports <= 64:
            raise NetError(f"{where}: {did} ports must be 1..64")
        self.net.devices[did] = dev
        self.net.counters[kind] = max(self.net.counters[kind], dev.num)
        self._settings(dev, spec, where)
        return dev

    def _auto(self, kind: str, spec: dict, where: str) -> Device:
        """The lowest free number of this kind, skipping every id the file fixes."""
        n = 1
        while f"{kind}{n}" in self.net.devices or f"{kind}{n}" in self.reserved:
            n += 1
        return self._declare(f"{kind}{n}", kind, spec, where)

    def _settings(self, dev: Device, spec: dict, where: str) -> None:
        net = self.net
        if dev.kind == "s" and not _onoff(spec.get("stp", self.default_stp), f"{where}.stp"):
            net.stp_off.add(dev.id)
        if "priority" in spec:
            net.stp_priority[dev.id] = int(spec["priority"])
        if dev.kind in "rg" and not _onoff(spec.get("dhcp", True), f"{where}.dhcp"):
            net.dhcp_off.add(dev.id)
        if "nat" in spec:
            if dev.kind != "g":
                raise NetError(f"{where}.nat: only gateways run NAT")
            words = spec["nat"] if isinstance(spec["nat"], list) else [spec["nat"]]
            for w in words:                       # YAML reads a bare on/off as true/false
                w = {True: "on", False: "off"}.get(w, str(w).lower()) if isinstance(w, bool) else str(w).lower()
                if w not in ("on", "off", "easy", "hard"):
                    raise NetError(f"{where}.nat: use on, off, easy or hard (or a list, e.g. [off, hard])")
                if w == "off":
                    net.nat_off.add(dev.id)
                elif w == "hard":
                    net.nat_hard.add(dev.id)
        mode = spec.get("interfaces", self.default_ifaces.get(dev.kind))
        if mode is not None:
            if mode not in ("dhcp", "adhoc", "none", "auto"):
                raise NetError(f"{where}.interfaces: use dhcp, adhoc, none or auto")
            for p in range(1, dev.nports + 1):
                net.ifaces[f"{dev.id}:{p}"] = Iface(mode)

    # ---- names → devices ---------------------------------------------------
    def _resolve(self, ref: Any, roles: dict[str, list[str]], where: str) -> list[tuple[str, int | None]]:
        if isinstance(ref, list):
            return [d for r in ref for d in self._resolve(r, roles, where)]
        ref = str(ref)
        if (m := INDEXED.match(ref)):
            role, i, port = m[1], int(m[2]), m[3]
            if role not in roles:
                raise NetError(f"{where}: no role {role!r} here")
            if i >= len(roles[role]):
                raise NetError(f"{where}: {role} has {len(roles[role])} devices, so {ref} doesn't exist")
            return [(roles[role][i], int(port) if port else None)]
        if ref in roles:
            return [(d, None) for d in roles[ref]]
        if (m := REF.match(ref)):
            did = f"{m[1].lower()}{int(m[2])}"
            if did not in self.net.devices:
                raise NetError(f"{where}: no device {did}")
            return [(did, int(m[3]) if m[3] else None)]
        if ref.lower() in self.net.groups:
            return [(d, None) for d in self.net.devices_in(ref.lower())]
        raise NetError(f"{where}: {ref!r} is not a device, role or group")

    def _links(self, items: list, roles: dict[str, list[str]], where: str) -> None:
        for n, item in enumerate(items or []):
            here = f"{where}[{n}]"
            if not isinstance(item, dict):
                raise NetError(f"{here}: expected e.g. {{mesh: [a, b]}}")
            role = str(item.get("role", self.default_role))
            if role not in ROLES:
                raise NetError(f"{here}: role must be one of {', '.join(ROLES)}")
            ops = [k for k in item if k != "role"]
            if len(ops) != 1:
                raise NetError(f"{here}: one of connect, mesh, zip, parallel, chain, ring")
            op, args = ops[0], item[ops[0]]
            try:
                if op == "parallel":
                    if not (isinstance(args, list) and len(args) == 3 and isinstance(args[2], int)):
                        raise NetError("parallel takes [a, b, count]")
                    sides = [self._resolve(x, roles, here) for x in args[:2]]
                    pairs = macro_pairs(op, sides, args[2])
                elif op in ("connect", "mesh", "zip"):
                    if not (isinstance(args, list) and len(args) == 2):
                        raise NetError(f"{op} takes two names")
                    pairs = macro_pairs(op, [self._resolve(x, roles, here) for x in args])
                elif op in ("chain", "ring"):
                    pairs = macro_pairs(op, [self._resolve(args, roles, here)])
                else:
                    raise NetError(f"unknown macro {op!r}")
            except NetError as e:
                raise NetError(f"{here}: {e}") from None
            for (da, pa), (db, pb) in pairs:
                try:
                    self.net.connect(da, pa, db, pb, role)
                except NetError as e:
                    raise NetError(f"{here}: {e}") from None

    # ---- groups --------------------------------------------------------------
    def _groups(self, tree: dict, roles: dict[str, list[str]], names: dict[str, str] | None,
                path: str, where: str) -> list[str]:
        """Create a group tree; returns the top-level group names in order.

        ``names`` maps template paths to chosen names; unmapped template groups
        take their key if it's free, else the next free variant (core → core-2).
        Stand-alone groups (``names`` is None) are named exactly by their key."""
        made = []
        for key, val in (tree or {}).items():
            local = f"{path}.{key}" if path else str(key)
            key = str(key).lower()
            claimed = frozenset(self.reserved_groups)
            try:
                if names is None:
                    name = self.net.resolve_group_name(key, claimed)
                elif local in names:
                    name = self.net.resolve_group_name(names[local], claimed)
                elif key not in self.net.groups and key not in self.net.devices and key not in claimed:
                    name = key
                else:                       # a template's own name, taken: use a free variant
                    name = self.net.next_group_name(key, claimed)
            except NetError as e:
                raise NetError(f"{where}.{local}: {e}") from None
            if not GROUP_NAME.match(name) or REF.match(name):
                raise NetError(f"{where}.{local}: bad group name {name!r} (start with a letter, "
                               f"at most 16 characters, not a device id)")
            if name in self.net.groups:
                raise NetError(f"{where}: group {name} is defined twice")
            members: list[str] = []
            subs: dict = {}
            cluster = None
            if isinstance(val, (str, list)):
                members = [d for d, _ in self._resolve(val, roles, f"{where}.{local}")]
            elif isinstance(val, dict):
                members = [d for d, _ in self._resolve(val.get("members", []), roles, f"{where}.{local}")]
                cluster = val.get("cluster")
                subs = {k: v for k, v in val.items() if k not in GROUP_KEYS}
            else:
                raise NetError(f"{where}.{local}: a group is a list of members or a mapping of subgroups")
            for d in members:                              # membership is a tree
                if (owner := self.net.group_of(d)) is not None or members.count(d) > 1:
                    raise NetError(f"{where}.{local}: {d} is already in group {owner or name}"
                                   f" (a device belongs to one group)")
            self.net.groups[name] = []                     # reserve before the children
            children = self._groups(subs, roles, names, local, where)
            self.net.groups[name] = children + members
            if not self.net.groups[name]:
                raise NetError(f"{where}.{local}: group {name} is empty")
            if cluster is not None:
                cluster = "none" if cluster is True else str(cluster)
                if cluster not in POLICIES:
                    raise NetError(f"{where}.{local}.cluster: one of {', '.join(POLICIES)} (or true)")
                self.net.clusters[name] = cluster
            mode = val.get("mode") if isinstance(val, dict) else None
            if mode is not None:
                if cluster is None or str(mode) not in CLUSTER_MODES:
                    raise NetError(f"{where}.{local}.mode: {' or '.join(CLUSTER_MODES)}, on a group with cluster:")
                if str(mode) == "active-passive":
                    self.net.cluster_modes[name] = "active-passive"
            made.append(name)
        return made

    @staticmethod
    def _explicit_group_names(spec: dict, instances: dict) -> set[str]:
        """Group names fixed in the file: instance names for template groups, wrapper
        groups, and every stand-alone group (nested ones included)."""
        out: set[str] = set()

        def fixed(name: Any) -> None:
            if name and not AUTO.match(str(name)):
                out.add(str(name).lower())

        for iname, inst in instances.items():
            inst = inst or {}
            fixed(inst.get("group", iname))
            for new in (inst.get("groups") or {}).values():
                fixed(new)

        def walk(tree: Any) -> None:
            for key, val in (tree or {}).items():
                fixed(key)
                if isinstance(val, dict):
                    walk({k: v for k, v in val.items() if k not in GROUP_KEYS})

        walk(spec.get("groups"))
        return out

    @staticmethod
    def _expand_ids(ids: Any, kind: str, where: str) -> list[str | None] | None:
        """Fixed ids, with auto-number patterns (s(2)) expanded to placeholders."""
        if ids is None:
            return None
        if not isinstance(ids, list):
            raise NetError(f"{where}: a list of ids, e.g. [{kind}1, {kind}2] or [{kind}(2)]")
        out: list[str | None] = []
        for d in ids:
            m = AUTO.match(str(d))
            if not m:
                out.append(str(d))
                continue
            if m[1].lower() != kind:
                raise NetError(f"{where}: this role holds {KINDS[kind][0]}s, so write {kind}({m[2]}), not {d}")
            out += [None] * int(m[2])
        return out

    @staticmethod
    def _group_paths(tree: dict, path: str = "") -> list[str]:
        out = []
        for key, val in (tree or {}).items():
            local = f"{path}.{key}" if path else str(key)
            out.append(local)
            if isinstance(val, dict):
                out += _Builder._group_paths({k: v for k, v in val.items() if k not in GROUP_KEYS}, local)
        return out

    # ---- the whole file ------------------------------------------------------
    def build(self) -> Network:
        spec, net = self.spec, self.net
        schema = spec.get("schema", SCHEMA)
        if not str(schema).startswith("bytesim/topology"):
            raise NetError(f"schema {schema!r} isn't a ByteSim topology")
        templates = spec.get("templates") or {}
        instances = spec.get("instances") or {}
        if "sites" in spec:
            raise NetError("use `instances:` for template instances (see docs/topology-format.md)")

        # Pass 1: every name the file fixes explicitly is reserved before anything is
        # allocated, so auto-numbering (s(2), fab(1)) never depends on what comes later.
        self.reserved = {str(d).lower() for d in (spec.get("devices") or {})}
        self.reserved_groups = self._explicit_group_names(spec, instances)
        for inst in instances.values():
            for ids in ((inst or {}).get("devices") or {}).values():
                self.reserved |= {str(d).lower() for d in ids if not AUTO.match(str(d))}

        for did, dspec in (spec.get("devices") or {}).items():
            m = REF.match(str(did))
            if not m:
                raise NetError(f"devices: {did!r} isn't a device id like r1 or s12")
            self._declare(str(did), m[1].lower(), dspec, f"devices.{did}")

        built: list[tuple[str, dict, dict, dict[str, list[str]]]] = []
        for iname, inst in instances.items():
            where = f"instances.{iname}"
            inst = inst or {}
            tname = inst.get("template")
            if tname not in templates:
                raise NetError(f"{where}: unknown template {tname!r}")
            tmpl = templates[tname] or {}
            fixed = inst.get("devices") or {}
            roles: dict[str, list[str]] = {}
            for role, rspec in (tmpl.get("devices") or {}).items():
                rspec = dict(rspec or {})
                kind = _kind(rspec.get("kind"), f"templates.{tname}.devices.{role}.kind")
                count = int(rspec.get("count", 1))
                here = f"{where}.devices.{role}"
                ids = self._expand_ids(fixed.get(role), kind, here)
                if ids is not None and len(ids) != count:
                    raise NetError(f"{here}: the template has {count}, you gave {len(ids)}")
                roles[role] = [self._declare(d, kind, rspec, here).id if d else self._auto(kind, rspec, here).id
                               for d in (ids or [None] * count)]
            unknown = set(fixed) - set(roles)
            if unknown:
                raise NetError(f"{where}.devices: the template has no role {sorted(unknown)[0]!r}")
            built.append((iname, tmpl, inst, roles))

        # Groups: instances first (each optionally wrapped), then stand-alone ones.
        for iname, tmpl, inst, roles in built:
            where = f"instances.{iname}"
            paths = self._group_paths(tmpl.get("groups") or {})
            names = {}
            for key, new in (inst.get("groups") or {}).items():
                hits = [p for p in paths if p == key or p.split(".")[-1] == key]
                if len(hits) != 1:
                    raise NetError(f"{where}.groups: {key!r} " + ("matches no group in the template" if not hits
                                   else f"is ambiguous, say which: {', '.join(hits)}"))
                names[hits[0]] = str(new).lower()
            tops = self._groups(tmpl.get("groups") or {}, roles, names, "", where)
            wrap = inst.get("group", iname)
            if wrap:
                try:
                    wrap = net.resolve_group_name(str(wrap), frozenset(self.reserved_groups))
                except NetError as e:
                    raise NetError(f"{where}.group: {e}") from None
                loose = [d for ids in roles.values() for d in ids if net.group_of(d) is None]
                net.group_add(wrap, tops + loose)
        self._groups(spec.get("groups") or {}, {}, None, "", "groups")

        # Cables, in file order: every instance's template links, then the stand-alone ones.
        for iname, tmpl, inst, roles in built:
            self._links(tmpl.get("links"), roles, f"instances.{iname}.links")
        self._links(spec.get("links"), {}, "links")
        for n, g in enumerate(spec.get("mlag") or []):
            g = str(g).lower()
            problem = net.mlag_problem(g)
            if problem:
                raise NetError(f"mlag[{n}]: {problem}")
            net.mlag_domains.add(g)
        for n, pair in enumerate(spec.get("lags") or []):
            where = f"lags[{n}]"
            if not (isinstance(pair, list) and len(pair) == 2):
                raise NetError(f"{where}: a pair of ends, e.g. [s8, s12] or [g1, fab1]")
            a, b = (str(x).lower() if str(x).lower() in net.mlag_domains
                    else self._resolve(x, {}, where)[0][0] for x in pair)
            problem = net.lag_problem(a, b)
            if problem:
                raise NetError(f"{where}: {problem}")
            net.lags.add(frozenset((a, b)))
        self._internet_and_overlays(spec)
        return net

    def _internet_and_overlays(self, spec: dict) -> None:
        """The public internet, relays and overlays (after groups and cables exist)."""
        net = self.net
        if spec.get("internet") is not None:
            g = str(spec["internet"]).lower()
            if g not in net.groups:
                raise NetError(f"internet: {g!r} isn't a group")
            net.internet = g

        def devices(ref: Any, where: str) -> list[str]:
            out = []
            for item in ref if isinstance(ref, list) else [ref]:
                for d, _ in self._resolve(item, {}, where):
                    if net.devices[d].kind == "s":
                        if str(item).lower() in net.groups:
                            continue                  # a group's switches are skipped
                        raise NetError(f"{where}: {d} is a switch")
                    if d not in out:
                        out.append(d)
            return out

        net.relays = devices(spec.get("relays") or [], "relays")
        for ov, t in (spec.get("overlays") or {}).items():
            where = f"overlays.{ov}"
            ov = str(ov).lower()
            if not GROUP_NAME.match(ov) or REF.match(ov):
                raise NetError(f"{where}: an overlay name starts with a letter and doesn't look like a device id")
            if ov in OVERLAY_RESERVED:
                raise NetError(f"{where}: {ov!r} is a word the overlay command uses: pick another name")
            t = t or {}
            unknown = set(t) - {"net", "members", "hosts", "coord", "exit", "lan", "routes", "acl"}
            if unknown:
                raise NetError(f"{where}: unknown key {sorted(unknown)[0]!r}")
            net.new_overlay(ov, int(t["net"]) if "net" in t else None)
            members = devices(t.get("members") or [], f"{where}.members")
            pinned = {}
            for d, h in (t.get("hosts") or {}).items():
                d = str(d).lower()
                if d not in members:
                    raise NetError(f"{where}.hosts: {d} isn't a member")
                pinned[d] = int(h)
            for d in [m for m in members if m in pinned] + [m for m in members if m not in pinned]:
                net.join_overlay(ov, d, pinned.get(d))
            net.overlays[ov]["members"] = members        # keep the file's order
            state = net.overlays[ov]
            for key in ("coord", "exit"):
                if t.get(key) is not None:
                    d = devices(t[key], f"{where}.{key}")
                    if len(d) != 1:
                        raise NetError(f"{where}.{key}: one device")
                    if key == "exit" and d[0] not in members:
                        raise NetError(f"{where}.exit: {d[0]} isn't a member")
                    state[key] = d[0]
            if _onoff(t.get("lan", False), f"{where}.lan"):
                state["lan"] = True
            if t.get("routes"):
                routes = devices(t["routes"], f"{where}.routes")
                if outside := [d for d in routes if d not in members]:
                    raise NetError(f"{where}.routes: {outside[0]} isn't a member")
                state["routes"] = routes
            if "acl" in t:
                rules = []
                for n, rule in enumerate(t["acl"] or []):
                    if not (isinstance(rule, list) and len(rule) == 2):
                        raise NetError(f"{where}.acl[{n}]: a pair [from, to], e.g. [c1, c3] or [site1, internet]")
                    ends = []
                    for x in rule:
                        x = str(x).lower()
                        if x not in ("*", "internet") and x not in net.groups:
                            x = self._resolve(x, {}, f"{where}.acl[{n}]")[0][0]
                        ends.append(x)
                    rules.append(ends)
                state["acl"] = rules


def load_topology(text: str) -> Network:
    """Build a Network from topology YAML (or JSON). Raises NetError with the place it broke."""
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise NetError(f"not valid YAML: {e}") from None
    if not isinstance(spec, dict):
        raise NetError("a topology file is a mapping (schema, devices, groups, links…)")
    return _Builder(spec).build()


def _kind(word: Any, where: str) -> str:
    w = str(word).lower()
    if w in KIND_NAME:
        return KIND_NAME[w]
    if w in ALIASES:
        return ALIASES[w]
    raise NetError(f"{where}: kind must be gateway, router, switch or computer")


def _onoff(v: Any, where: str) -> bool:
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("on", "enabled", "yes", "true"):
        return True
    if str(v).lower() in ("off", "disabled", "no", "false"):
        return False
    raise NetError(f"{where}: use on or off")


# ---------------------------------------------------------------------------- export
def dump_topology(net: Network, pin_ports: bool = False) -> str:
    """Write a network as a topology file (no templates: those can't be inferred).

    Compact by default: meshes between groups and parallel cables are folded
    into macros, and ports are left to be allocated on load. ``pin_ports``
    writes every cable as an explicit ``connect`` with its ports, so loading it
    rebuilds identical cabling (and so identical MACs on each end)."""
    out = [f"schema: {SCHEMA}",
           "# Exported by ByteSim. Positions, addresses, failures and storms are not included.",
           ""]
    stp_default = len(net.stp_off) <= sum(1 for d in net.devices.values() if d.kind == "s") / 2
    out.append(f"defaults: {{links: data, stp: {'on' if stp_default else 'off'}}}")
    out.append("")

    out.append("devices:")
    for kind in KINDS:
        devs = [d for d in net.sorted_devices() if d.kind == kind]
        if not devs:
            continue
        name = KINDS[kind][0]
        out.append(f"  # {name}{'es' if name.endswith('h') else 's'}")
        for d in devs:
            extra = {}
            if d.kind == "s" and (d.id in net.stp_off) == stp_default:
                extra["stp"] = "off" if d.id in net.stp_off else "on"
            if d.id in net.stp_priority:
                extra["priority"] = net.stp_priority[d.id]
            if d.id in net.dhcp_off:
                extra["dhcp"] = "off"
            nat = [w for w, on in (("off", d.id in net.nat_off), ("hard", d.id in net.nat_hard)) if on]
            if nat:
                extra["nat"] = nat[0] if len(nat) == 1 else f"[{', '.join(nat)}]"
            modes = {i.mode for p in range(1, d.nports + 1) if (i := net.ifaces.get(f"{d.id}:{p}"))}
            if d.kind == "c" and modes and modes != {"dhcp"} and len(modes) == 1 and "static" not in modes:
                extra["interfaces"] = modes.pop()
            if extra:
                inner = ", ".join([f"ports: {d.nports}"] + [f"{k}: {v}" for k, v in extra.items()])
                out.append(f"  {d.id}: {{{inner}}}")
            else:
                out.append(f"  {d.id}: {d.nports}")

    if net.groups:
        out += ["", "groups:"]
        for g in net.groups:
            if net.parent_of(g) is None:
                out += _dump_group(net, g, 1)

    out += ["", "links:"]
    out += _dump_links(net, pin_ports)
    if net.mlag_domains:
        out += ["", f"mlag: [{', '.join(sorted(net.mlag_domains))}]"]
    if net.lags:
        out += ["", "lags:"]
        for pair in sorted(net.lags, key=lambda p: sorted(p, key=net._end_order)):
            a, b = sorted(pair, key=net._end_order)
            out.append(f"  - [{a}, {b}]")
    if net.internet:
        out += ["", f"internet: {net.internet}"]
    if net.relays:
        out += ["", f"relays: [{', '.join(net.relays)}]"]
    state = net.overlay_state()
    if state:
        out += ["", "overlays:"]
        for ov in sorted(state, key=lambda t: state[t]["net"]):
            t = state[ov]
            ordered = sorted(t["members"], key=t["hosts"].get)
            # Host numbers are written only where joining in order wouldn't give them.
            pinned = {d: h for d, h in t["hosts"].items() if ordered.index(d) + 1 != h}
            out += [f"  {ov}:", f"    net: {t['net']}", f"    members: [{', '.join(ordered)}]"]
            if pinned:
                out.append("    hosts: {" + ", ".join(f"{d}: {h}" for d, h in pinned.items()) + "}")
            for key in ("coord", "exit"):
                if t.get(key):
                    out.append(f"    {key}: {t[key]}")
            if t.get("lan"):
                out.append("    lan: on")
            if t.get("routes"):
                out.append(f"    routes: [{', '.join(t['routes'])}]")
            if "acl" in t:
                rules = list(dict.fromkeys(map(tuple, t["acl"])))
                out.append("    acl: [" + ", ".join(f"['{a}', '{b}']" for a, b in rules) + "]")
    return "\n".join(out) + "\n"


def _dump_group(net: Network, g: str, depth: int) -> list[str]:
    pad = "  " * depth
    subs = net.subgroups(g)
    devs = [m for m in net.groups[g] if m in net.devices]
    cluster = net.clusters.get(g)
    if not subs and cluster is None:
        return [f"{pad}{g}: [{', '.join(devs)}]"]
    lines = [f"{pad}{g}:"]
    if cluster is not None:
        lines.append(f"{pad}  cluster: {cluster}")
        if g in net.cluster_modes:
            lines.append(f"{pad}  mode: {net.cluster_modes[g]}")
    if devs:
        lines.append(f"{pad}  members: [{', '.join(devs)}]")
    for sub in subs:
        lines += _dump_group(net, sub, depth + 1)
    return lines


def _dump_links(net: Network, pin_ports: bool) -> list[str]:
    def end(d: str, p: int) -> str:
        return f"{d}:{p}" if pin_ports else d

    lines: list[str] = []
    if pin_ports:
        for l in net.links:
            role = f", role: {l.role}" if l.role != "data" else ""
            lines.append(f'  - {{connect: [{end(l.a, l.pa)}, {end(l.b, l.pb)}]{role}}}')
        return lines

    # Cables as a multiset of unordered device pairs, per role.
    remaining = [(min(l.a, l.b, key=_order), max(l.a, l.b, key=_order), l.role) for l in net.links]
    data_pairs: dict[tuple[str, str], int] = {}
    for a, b, role in remaining:
        if role == "data":
            data_pairs[(a, b)] = data_pairs.get((a, b), 0) + 1

    def take(a: str, b: str) -> None:
        key = (min(a, b, key=_order), max(a, b, key=_order))
        data_pairs[key] -= 1
        if not data_pairs[key]:
            del data_pairs[key]

    def has(a: str, b: str) -> bool:
        return data_pairs.get((min(a, b, key=_order), max(a, b, key=_order)), 0) > 0

    # Meshes between groups' direct devices (largest first), then parallels, then singles.
    sets = sorted(((g, [m for m in net.groups[g] if m in net.devices]) for g in net.groups),
                  key=lambda t: -len(t[1]))
    sets = [(g, ds) for g, ds in sets if len(ds) >= 1 and not net.subgroups(g)]
    for i, (ga, a) in enumerate(sets):
        for gb, b in sets[i + 1:]:
            if set(a) & set(b) or len(a) * len(b) < 2:
                continue
            if all(has(x, y) for x in a for y in b):
                for x in a:
                    for y in b:
                        take(x, y)
                lines.append(f"  - mesh: [{ga}, {gb}]")
    for (a, b), n in sorted(data_pairs.items(), key=lambda kv: (_order(kv[0][0]), _order(kv[0][1]))):
        lines.append(f"  - parallel: [{a}, {b}, {n}]" if n > 1 else f"  - connect: [{a}, {b}]")
    for a, b, role in remaining:
        if role != "data":
            lines.append(f"  - {{connect: [{a}, {b}], role: {role}}}")
    return lines


def _order(did: str) -> tuple[int, int]:
    return RANK[did[0]], int(did[1:])
