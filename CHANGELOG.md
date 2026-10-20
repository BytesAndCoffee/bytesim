# Changelog

## 0.1.0 (unreleased)

First release.

- Gateways, routers, switches and computers with any number of ports or NICs;
  cabling by typing device ids.
- Automatic layered layout; moving devices with the keyboard or mouse; panning.
- Selectable tree, circular and grid layouts alongside the default layered
  arrangement (`layout <mode>`, `layout list`); nested groups stay together,
  and the selected mode is retained in JSON saves and undo/redo.
- Per-group horizontal or vertical flow (`group <name> layout=vertical`),
  including nested groups; orientations survive saves, undo/redo and cloning.
- `layout` accepts a layout and/or orientation in either order
  (`layout grid vertical`, `layout vertical`); per-group settings take precedence.
- `layout compact`: tighter spacing and height-based packing of devices and
  nested group blocks, with orientation overrides and snapshot persistence.
- Orientation preserves each layout's structure: layered/tree levels and
  branches, wrapping rows/columns for grid/compact, and rings for circular.
- `layout compact-layered`: tighter block and level spacing while preserving
  layered device-kind ordering, branches, nested groups and orientation.
- Nestable groups drawn as frames.
- Addressing as (network, 8-bit host); MACs named after their port; router
  autoconfiguration, animated DHCP, ad-hoc fallback, static addresses.
- Routing through gateways and between routers (cheapest path in cable hops),
  with ARP shown for each hop and MAC learning on switches.
- Ping, unicast and multicast with packet animation; graph mode for pure
  connectivity.
- Cable roles (data, control, fabric) and failure injection.
- HA router clusters: virtual gateways, failover, split-brain detection,
  quorum and witness policies.
- BFS/DFS renumbering, one-shot or automatic (`renumber [auto] bfs|dfs [root]`).
- Spanning tree (root bridges, blocked ports, failover, priorities), real
  flooding, and broadcast storms with MAC flapping; `broadcast`, `stp`,
  `demo loop`.
- `clone <group>`: copy a group's devices, nested groups and internal cables.
- Declarative YAML topologies (`bytesim/topology-v1`): templates, instances,
  link macros; `load` reads them and `export` writes them (compact or exact).
- Auto-numbering with `prefix(n)` (`s(2)`, `fab(1)`) in topology files and in
  `group` / `clone`.
- `ports copy a(x, y) b(y, z)`: give a group another group's outside cabling onto new
  endpoints; `copy a(x, y) b(y, z)` does it while cloning.
- `export script [readable|shortest|explicit] [file]`: synthesize the console commands that
  rebuild the network, reusing repeated groups with clone / copy / ports copy; every
  script is verified by replaying it. `run <file>` replays scripts.
- Multi-level undo and redo (`undo [n]`, `redo [n]`, `history`, `u` / `U`), up to 100 steps.
- Random background traffic (`traffic on [rate] [seed=] [hosts=] [mix=]`) with cable load
  shading, drop reasons and statistics.
- Active-passive HA clusters (`cluster … mode=active-passive`): only the active member forwards;
  the standby takes over on failure or election. `demo core`: three sites through a redundant
  routed core.
- Router autoconfiguration keeps every network id on one segment: stale ids left by
  re-cabling are renumbered instead of being shared by separate links.
- ECMP: equal-cost routes are chosen per flow (stable hash of source and destination), so
  flows spread while each stays on one path.
- Link aggregation (`lag a b`, `unlag`, `lags`): parallel switch cables act as one link: not
  blocked by STP, not a loop, flows hashed across members, survives member failures.
- Link aggregation also works between a switch and a router or gateway, which then treats the
  members as one interface with one address. `lag` finds aggregatable spans, `lag auto` bundles
  them, and cabling a second parallel cable suggests it.
- MLAG (`mlag <group>`, like vPC): a group of peer-linked switches acts as one logical switch,
  so a device's cables to several of them can be bundled (`lag g1 fab1`, found by `lag`).
  Local forwarding preference and vPC loop avoidance included.
- Cabling-pattern commands (`mesh`, `zip`, `parallel`, `chain`, `ring`), sharing one
  implementation with the YAML link macros; `ports` and `mlag` take several devices / groups.
- Shorter `export script`: cabling patterns, runs of `add` with port fixes, one `mlag` line,
  `lag auto`, and clones of groups that contain MLAG domains (a 48-device MLAG fabric went
  from 168 to 80 commands).
- JSON save and load; a `bytesim` command with `--demo` and `--run`.
- Scoped port/adjacency indexes, per-route query caches and reused cable
  geometry reduce routing overhead on large fabrics.
