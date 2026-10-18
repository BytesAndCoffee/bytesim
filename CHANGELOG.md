# Changelog

## 0.1.0 (unreleased)

First release.

- Gateways, routers, switches and computers with any number of ports or NICs;
  cabling by typing device ids.
- Automatic layered layout; moving devices with the keyboard or mouse; panning.
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
- JSON save and load; a `bytesim` command with `--demo` and `--run`.
