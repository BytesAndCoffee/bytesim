# Changelog

## Unreleased

### The internet and NAT
- `internet <group>` declares the public internet. Gateways cabled to it (their
  WAN ports are those with the shortest cabled path to it) run NAT: traffic goes
  out as the gateway's WAN address, unsolicited inbound traffic is dropped with
  the reason, and replies get back in. Works through private uplink routers and
  double NAT; a second gateway doesn't turn a LAN into a WAN.
- `nat <gateway…> on|off|easy|hard`; NAT drops are their own traffic statistic.
- `demo internet`: three sites behind NAT, an ISP and a public server.

### Overlay networks
- `overlay <name> <devices|groups…>`: mesh-VPN overlays. Members
  get stable overlay addresses (`(100,n)`, `c3@ol`) and reach each other through
  NAT by hole punching, shown hop by hop with the WireGuard endpoints.
- Easy and hard NAT, relays (`relay <device>`) for pairs that can't go direct,
  and a coordination server (`coord=`) that new members must reach to join while
  existing tunnels survive it being down.
- Subnet routers (`overlay <name> advertise <member>`), an exit node
  (`exit=<member>`, with `lan=on` to keep own-site traffic local) and access
  rules (`acl <overlay> allow <from> <to>`, deny by default once any exist).
- `overlay <name>` lists members with their endpoint and NAT situation, and every
  pair's connection (direct, relayed, denied) for small overlays;
  `overlay <name> peers <member>` shows one member's view.
- Overlay targets in multicast ping (`ping c1 all@ol`, replies come back through
  the tunnel) and in graph mode.
- `demo overlay`; all of it is kept by saves, undo, export scripts and YAML topologies.

### Forwarding fidelity
- Ping replies route on their own instead of retracing the request: a receiver
  with no way back (no gateway, say) now gives `✗ no reply` rather than a reply.
  The reported TTL is the reply's.
- TTLs are enforced: the 64th router on the way drops the packet (time exceeded).
- An active-passive standby no longer forwards for hosts that use its own
  address as their gateway.
- A source interface (`ping c1:2 c3`, `unicast c1:2 c3`, `mping c1:2 …`) binds
  the packet to that NIC instead of being ignored. Routers and graph mode refuse one.
- Switches forward by their MAC tables: every hop's ARP request teaches the
  switches it floods the sender's MAC, the reply teaches its path the answer's,
  and the frame follows those entries (flooding unknown destinations, ignoring
  entries on dead cables). `Network.forward()` follows the tables as they are.
- ARP for an address nobody has is sent before it fails, from the host (on-link)
  or from the router on that network, so it floods and can set off a storm.

## 0.3.2 (2026-10-20)

- `demo mlag` (`bytesim --demo mlag`): a seven-tier data centre (edge gateway,
  clustered routers, then spine, aggregation, leaf and rack switches and
  computers in two pods). Spine, aggregation and leaf are MLAG pairs, racks are
  dual-homed to their leaf pair, and every span between tiers is one bundle.
  Nothing is STP-blocked, and any single router, spine/aggregation/leaf switch
  or uplink cable can fail without cutting off a rack; a rack switch remains
  its own rack's single point of failure.

## 0.3.1 (2026-10-20)

- Smoother animation on large topologies: the canvas renders line by line from a
  cached frame grid, caches group frames and address chains with the cable
  geometry, and settles the garbage collector after each network load. On a
  88-device fabric, frame draws dropped from about 12 ms to 4 ms and traffic
  playback went from 27 to 60 ticks per second.

## 0.3.0 (2026-10-20)

### Layout
- Selectable tree, circular and grid layouts alongside the default layered
  arrangement (`layout <mode>`, `layout list`); nested groups stay together,
  and the selected mode is retained in JSON saves and undo/redo.
- `layout compact`: tighter spacing and height-based packing of devices and
  nested group blocks. `layout compact-layered`: tighter block and level spacing
  while preserving layered device-kind ordering, branches and nested groups.
- Per-group horizontal or vertical flow (`group <name> layout=vertical`),
  including nested groups; orientations survive saves, undo/redo and cloning.
- `layout` accepts a layout and/or orientation in either order
  (`layout grid vertical`, `layout vertical`); per-group settings take precedence.
- Orientation preserves each layout's structure: layered/tree levels and
  branches, wrapping rows/columns for grid/compact, and rings for circular.

### Packets
- Multicast ping (`mping`, `mcast ping`, or `ping` with groups / `all` / several
  destinations): a shared request, independently routed replies, per-receiver
  failures and a reply summary, in address and graph modes.
- `mping all <target>` sends from every computer; `mping all all` tests all
  distinct computer pairs; `mping all all *` includes every addressed computer,
  router and gateway (all nodes in graph mode). Explicit interface/address targets
  survive multicast expansion, with deterministic shortest-route/lowest-port selection.
- Packet animation targets 60 fps, preserving normal playback duration, arrival
  flashes and storm effect timing.
- Adjustable packet animation (`speed slow|normal|fast|<multiplier>`): a shared
  pace scales to long paths while preserving multicast branching. Normal speed
  targets about 1.5 seconds per longest active one-way path.
- `pause` / `resume` (`play`): freeze in-flight packets and background traffic,
  queue packet commands, and launch the batch together. `queue` lists pending
  commands; `queue clear` discards them. Network replacement clears the queue.

### Output
- Global `trace on/off` and `detail on/off/auto` settings, with matching startup
  flags. They apply to subsequent output, including in-flight replies, persist
  across network loads and are visible in the inspector.
- `trace on`: full per-receiver request and independently routed reply paths with
  ports, cable roles, link numbers, endpoint addresses and failure reasons. Works
  with summaries, whole-network batches, queues and tee logs.
- Console tee (`tee <file>`, `tee`, `tee off`) and `--log-file`: append plain
  UTF-8 console output, including later packet replies, while keeping it visible.
  Logs close on exit; failed writes disable capture without interrupting simulation.

### Networking
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

### Performance
- Large multicast batches prepare and process arrivals in bounded work slices,
  with simultaneous request starts, pause support and per-sender summaries
  (`detail on` for full logs). Network/layout edits cancel obsolete packet work.
- Scoped port/adjacency indexes, per-route query caches, reused cable geometry
  and linear-time packet completion reduce all-to-all overhead; faster traffic
  on large fabrics.

## 0.1.0 (2026-10-17)

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
