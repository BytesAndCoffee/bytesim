# Topology format (`bytesim/topology-v1`)

A topology file describes a network's **architecture**: devices, groups and
cables. It can use reusable templates and link macros, and it is meant to be
written and read by people. It's YAML (JSON also works).

- `load file.yaml` or `bytesim file.yaml` builds the network and auto-arranges
  it.
- `export file.yaml` writes the current network as a topology.
- `save` / `load` with JSON remain the complete snapshot (positions, addresses,
  failures). See [file-format.md](file-format.md).

A topology deliberately leaves out runtime state: positions (the layout is
recomputed), addresses (re-learned by DHCP and router autoconfiguration),
failures and storms.

## A complete example

```yaml
schema: bytesim/topology-v1

defaults:
  links: data                         # role of cables that don't say otherwise
  stp: on                             # switches run spanning tree unless they say otherwise
  interfaces: {computer: dhcp}        # interface mode per device kind

templates:
  site:
    devices:                          # roles: a name for a set of devices
      routers:  {kind: router,   count: 2, ports: 6}
      dist:     {kind: switch,   count: 2, ports: 6}
      access:   {kind: switch,   count: 2, ports: 4}
      pcs:      {kind: computer, count: 2}
    groups:                           # local names; instances can rename them
      core:   {cluster: witness, members: [routers]}
      fabric: dist
      office:
        room1: ["access[0]", "pcs[0]"]
        room2: ["access[1]", "pcs[1]"]
    links:
      - mesh: [routers, dist]
      - mesh: [dist, access]
      - connect: ["access[0]", "access[1]"]
      - zip: [pcs, access]
      - {connect: ["routers[0]:6", "routers[1]:6"], role: control}

instances:
  north:                              # wrapped in a group called "north"
    template: site
    devices: {routers: [r1, r2]}      # fix some ids; the rest are numbered automatically
    groups: {core: rcore1, office.room1: lab}
  south:
    template: site

devices: {s99: {ports: 4, stp: off}}  # stand-alone devices
groups:
  transit: [s99]
links:
  - mesh: [[r1, r3], s99]
```

## Sections

### `schema`
`bytesim/topology-v1`. Optional, but it lets ByteSim tell a topology from a
save file without guessing.

### `defaults`
| Key | Values | Default |
|---|---|---|
| `links` | `data`, `control`, `control-backup`, `fabric` | `data` |
| `stp` | `on` / `off` | `on` |
| `interfaces` | kind → `dhcp`, `adhoc`, `none`, `auto` | computers `dhcp`, routers and gateways `auto` |

Kinds can be written `gateway`, `router`, `switch`, `computer` (or `g`, `r`,
`s`, `c`, `pc`…).

### Devices
A device is declared by its id. The letter must match the kind: `r` router,
`s` switch, `g` gateway, `c` computer.

```yaml
devices:
  s8: 5                                       # shorthand for {ports: 5}
  s12: {ports: 5, stp: off, priority: 4096}
  r9: {ports: 8, dhcp: off}
  c7: {interfaces: adhoc}
```

| Key | Applies to | Meaning |
|---|---|---|
| `ports` | all | port / NIC count, 1–64 (defaults: gateway 4, router 4, switch 8, computer 1) |
| `stp` | switches | `on` / `off` (overrides `defaults.stp`) |
| `priority` | switches | STP bridge priority (lower wins the root election) |
| `dhcp` | routers, gateways | `off` switches the DHCP server off |
| `interfaces` | all | address mode for every port |

### Templates
A template has `devices` (roles), `groups` and `links`. A role is
`{kind, count, ports, …}`, plus any of the device keys above.

### Instances
```yaml
instances:
  <name>:
    template: <template>
    group: <name> | false     # wrap the instance in a group; default: the instance's own name
    devices: {role: [ids…]}   # optional: fixed ids, one per device in the role
    groups: {key: name}       # optional: names for the template's groups
```

- **Ids:** fixed ids are reserved across the whole file first. Every other
  device takes the lowest free number of its kind.
- **Group names:** a key in `groups:` matches a template group by its own key
  (`office`) or by dotted path (`office.room1`). Use the path when a key
  appears more than once (`edge.fabric`, `intermediate.fabric`). Unnamed
  template groups keep their key if it's free, else the next free variant
  (`fabric` → `fabric-2`).

### Auto-numbering: `prefix(n)`
Anywhere the file *declares* a name, `prefix(n)` stands for n new names, each
the prefix plus the lowest number not already taken:

```yaml
instances:
  site3:
    template: site
    group: site(1)                       # the next free siteN
    devices:
      gateways:     [g7, g(2)]           # g7 fixed, then two new gateways
      distribution: [s(2)]               # two new switches
    groups:
      edge.fabric: fab(1)                # fab5 if fab1–fab4 exist
      intermediate.fabric: fab(1)        # then fab6
groups:
  pool(1): [s50]                         # stand-alone groups too
```

- **Devices:** the prefix is the kind letter of the role, so `s(2)` only in a
  switch role. Auto ids never take an id that's fixed anywhere in the file.
- **Groups** take `(1)`, since a name stands for one group. A prefix that
  would make device-like names (`s(1)` → `s1`) is refused.
- Names are handed out in file order, so two `room(1)` in one instance become
  consecutive numbers.
- **Explicit names always win, wherever they appear.** Before anything is
  allocated, every id and group name the file fixes explicitly is reserved. An
  `s(3)` near the top never takes an `s8` declared further down, and a
  `fab(1)` never takes a `fab1` named by a later instance or stand-alone group.
  With `s1 s2 s4 s7` declared and `s5` fixed later, `s(3)` gives `s3 s6 s8`.
- **Template groups you don't name** keep their own key (`office`) if nothing
  else claims it, else they take a free variant (`office-2`).
- **Stability:** a topology file is always expanded into an empty network, so
  the same file always produces the same names, and nothing in a running
  session affects it. Editing the file can shift auto-numbered names, though:
  fixing a new `s5`, or removing one, moves the gaps that `s(n)` fills. Where
  other things depend on a name, write it explicitly, or `export` the network
  (exports always use concrete ids).

The console's `group` and `clone` commands accept the same syntax:
`group lab(1) c1 c2`, `clone fab1 fab(1)`.

### Groups
Under `groups:` (stand-alone, or in a template):

- a **list** (or a single name) gives the members: `link1: [s8, s12]`. A
  device can belong to only one group;
- a **mapping** gives subgroups, optionally with `members:` for devices that
  belong directly, and `cluster:` (`none`, `quorum`, `witness`, or `true`) to
  make the group an HA cluster.

### Links
Each item is one macro, optionally with `role:`:

| Macro | Makes |
|---|---|
| `connect: [a, b]` | one cable between two single devices |
| `mesh: [A, B]` | a cable between every device of A and every device of B |
| `zip: [A, B]` | A[0]–B[0], A[1]–B[1]… (A and B must be the same size) |
| `parallel: [a, b, n]` | n separate cables between a and b |
| `chain: [a, b, c…]` | a–b, b–c, … |
| `ring: [a, b, c…]` | a chain, closed back to the start |

**Names** in a macro can be:

- a device id (optionally pinned to a port: `r1:5`);
- inside a template, a role (`routers`) or one device of it (`routers[0]`,
  `routers[0]:6`);
- a group: every device in it, nested groups included;
- a list of any of these: `[r1, r2]`.

**Ports** are allocated in file order: every instance's template links, in
instance order, then the stand-alone `links:`. Each cable takes the first
free port at each end unless pinned. Pinned ports must be free.

## Export

```text
export net.yaml          compact
export net.yaml ports    exact
```

- **Compact:** complete meshes between groups become `mesh:` macros, and
  repeated cables become `parallel:`. Ports are left to be allocated on load,
  so the cabling between devices is the same but port numbers can differ.
- **Exact:** every cable is written as `connect: [a:p, b:q]`, so loading it
  rebuilds identical cabling, and so identical MACs on each end.

Both keep devices and port counts, groups, clusters and their policies, cable
roles, and STP and DHCP-server settings. Export never invents templates: it
can't know which parts of your network you consider "the same".

## Errors

Mistakes are reported with their place in the file, for example:

```text
instances.site1.groups: 'fabric' is ambiguous, say which: edge.fabric, intermediate.fabric
instances.site1.links[5]: zip needs equal sizes (2 vs 3)
links[1]: s1 has no free ports (try `ports s1 2`)
```
