# Save file format

This is the complete JSON snapshot written by `save`. For the hand-writable,
template-based YAML format, see [topology-format.md](topology-format.md).

`save` writes, and `load` reads, a single JSON object. All keys except
`counters`, `devices` and `links` are optional, so older files still load.

```json
{
  "layout_mode": "layered",
  "group_orientation": "auto",
  "counters": { "g": 1, "r": 2, "s": 3, "c": 10 },
  "devices": [
    { "kind": "r", "num": 1, "nports": 6, "x": 64, "y": 1, "up": true }
  ],
  "links": [
    { "a": "r1", "pa": 5, "b": "r2", "pb": 5, "role": "control", "up": true }
  ],
  "groups":   { "dc1": ["fab1", "rfab1"], "rfab1": ["r1", "r2"], "fab1": ["s1", "s2"] },
  "group_layouts": { "fab1": "vertical" },
  "clusters": { "rfab1": "witness" },
  "cluster_modes": { "rfab1": "active-passive" },
  "ifaces": {
    "c1:1": { "mode": "dhcp", "net": 1, "host": 10, "gw": 254, "via": "dhcp" }
  },
  "dhcp_off": ["r2"],
  "lags": [["s8", "s12"], ["fab1", "g1"]],
  "mlag_domains": ["fab1"],
  "stp_off": ["s4", "s5"],
  "stp_priority": { "s7": 4096 },
  "stp_cost": { "s7:3": 2000 },
  "static_routes": { "r1": { "5": [2, 2], "default": "drop" } },
  "internet": "isp",
  "nat_off": ["g6"],
  "nat_hard": ["g4", "g5"],
  "overlays": {
    "ol": { "net": 100, "members": ["c1", "c3", "c7"], "hosts": { "c1": 1, "c3": 2, "c7": 4 },
            "coord": "c7", "exit": "c7", "lan": true, "routes": ["c3"], "acl": [["c1", "c3"], ["site1", "internet"]] }
  },
  "relays": ["c8"]
}
```

| Key | Meaning |
|---|---|
| `layout_mode` | selected arrangement: `layered`, `tree`, `circular`, `grid`, `compact` or `compact-layered` (absent in older files: `layered`). Positions are still restored exactly; auto-arrange starts off when opening a snapshot |
| `group_orientation` | layout flow direction, inherited by groups: `auto`, `horizontal` or `vertical` (absent in older files: `auto`). Preserves the selected algorithm's structure. Explicit `group_layouts` entries override direction within a group |
| `counters` | the last number used for each kind, so ids are never reused |
| `devices[]` | `kind` (`g`/`r`/`s`/`c`) and `num` make the id (`r1`). Also `nports`, canvas position `x`/`y`, and `up` (false = failed) |
| `links[]` | two ends (`a`/`pa`, `b`/`pb`; ports start at 1), `role` (`data`, `control`, `control-backup`, `fabric`) and `up` |
| `groups` | group → members, in order. A member is a device id or another group's name (groups nest) |
| `group_layouts` | group → `horizontal` or `vertical`, orienting the selected algorithm inside that group (absent: follow `group_orientation`). Entries for missing groups are dropped |
| `clusters` | groups that are HA clusters → split policy (`none`, `quorum`, `witness`) |
| `cluster_modes` | clusters where only the active member forwards → `active-passive` (absent: active-active) |
| `ifaces` | `"device:port"` → address config. `mode`: `dhcp`, `adhoc`, `static`, `auto` (routers) or `none`. `net`/`host`: the address. `gw`: the gateway's host number on the same network (254 = cluster virtual gateway). `via`: how the current address was obtained |
| `dhcp_off` | routers and gateways whose DHCP server is switched off |
| `lags` | bundles: pairs of ends (a device, or an MLAG domain) whose data cables act as one link |
| `mlag_domains` | groups whose switches act as one logical switch (multi-chassis aggregation) |
| `stp_off` | switches with spanning tree switched off (every other switch runs it) |
| `stp_priority` | switch → bridge priority (default 32768; lower wins the root election) |
| `static_routes` | router → network (or `default`) → next-hop address `[net, host]`, or `"drop"` |
| `stp_cost` | `"switch:port"` → STP path cost (default 20000, as for 1 Gb/s; lower is preferred toward the root) |
| `internet` | the group that is the public internet (absent: none, so gateways lead to an implicit internet) |
| `nat_off` | gateways facing the internet that don't run NAT |
| `nat_hard` | gateways whose NAT is hard (hole punching to another hard NAT fails) |
| `overlays` | overlay → `net` (overlay network, 100–109), `members` (join order), `hosts` (member → overlay host number; missing ones are numbered in join order), and optionally `coord`, `exit`, `lan`, `routes` (subnet routers) and `acl` (`[from, to]` pairs; an empty list allows nothing, a missing one everything) |
| `relays` | devices that relay overlay traffic, in preference order |

Runtime state isn't saved: MAC tables, ARP caches, NAT mappings and the clock
start empty when a file is loaded, as after a restart. Blocked ports and storms aren't saved either: blocked ports are recomputed when a file
is loaded, and a storm only starts when traffic is sent.

Entries that refer to missing devices or groups are dropped on load, and so
are overlays that don't make sense (bad names, switches as members, duplicate
networks or host numbers are dropped or renumbered). An
address in an older format is also dropped, and recomputed from the cabling.
