# ByteSim user guide

- [The screen](#the-screen)
- [Devices and ports](#devices-and-ports)
- [Cabling](#cabling)
- [Layout and moving things](#layout-and-moving-things)
- [Groups](#groups)
- [Renumbering (BFS / DFS)](#renumbering-bfs--dfs)
- [Addressing](#addressing)
- [Sending traffic](#sending-traffic)
- [Random traffic](#random-traffic)
- [Loops, spanning tree and broadcast storms](#loops-spanning-tree-and-broadcast-storms)
- [Cable roles and failures](#cable-roles-and-failures)
- [HA router clusters](#ha-router-clusters)
- [Routed cores vs shared fabrics](#routed-cores-vs-shared-fabrics)
- [Graph mode](#graph-mode)
- [Undo](#undo)
- [Saving and loading](#saving-and-loading)
- [Command reference](#command-reference)
- [Keys and mouse](#keys-and-mouse)
- [What is simplified](#what-is-simplified)

## The screen

```
┌ network ───────────────────────────────┐┌ inspector ───────────┐
│                                        ││ mode: addr · auto: on│
│   devices, cables, group frames,       ││ the selection: ports,│
│   packets in flight                    ││ MACs, addresses, HA  │
│                                        ││ state, MAC tables    │
└────────────────────────────────────────┘└──────────────────────┘
┌ console ─────────────────────────────────────────────────────────┐
│ results, errors, ARP lines, DHCP exchanges, HA state changes     │
└──────────────────────────────────────────────────────────────────┘
 › command line (↑/↓ for history)
```

The command line has focus when ByteSim starts. `Esc` moves focus to the
canvas, and `Enter` or `:` on the canvas moves it back.

## Devices and ports

| Kind | Id | Default ports | Colour |
|---|---|---|---|
| gateway | `g1`, `g2`… | 4 | magenta |
| router | `r1`… | 4 | cyan |
| switch | `s1`… | 8 | green |
| computer | `c1`… | 1 NIC | blue |

Ids count up separately for each kind and are never reused, so after deleting
`r2` the next router is still `r3`. Any device can have 1–64 ports.

```text
add r              a router with the default 4 ports
add s 24           a 24-port switch
add c 2 3          three computers, each with 2 NICs
ports r1 8         change r1 to 8 ports (it can't drop a port that's in use)
del c3             delete a device and its cables
```

Each box shows its ports as `○` (free) or `●` (cabled). Ports are numbered
from 1, left to right. Computers call them `nic1`, `nic2`…; everything else
calls them `p1`, `p2`…

## Cabling

Type two device ids to connect them:

```text
r1 s1              first free port on each
r1:3 s1:8          specific ports (c2:nic2 and r1:p3 also work)
unlink r1 s1       remove every cable between r1 and s1
unlink c1:2        remove the cable on c1's second NIC
```

Any two devices can be cabled, and several cables can join the same pair.
Cables leave from their own port and are routed around the boxes
automatically.

## Layout and moving things

With **auto-arrange** on (the default), every change re-lays out the network:

- Gateways go on the top row, then routers, switches, and computers at the
  bottom. A chain of devices of the same kind (`r1`–`r2`, or one switch
  feeding another) gets an extra row.
- Devices in each row are ordered to reduce crossing cables, then shifted to
  line up with what they connect to.
- Members of a group stay together, as described below.

To move things yourself, select a device and use the arrow keys (hold Shift to
move 4 cells at a time), or drag the box with the mouse. To select a device,
type its id on its own, press `Tab`, or click it. The first manual move turns
auto-arrange off. Press `a` or type `auto` to turn it back on.

Pan the view with `h` `j` `k` `l`, the mouse wheel (Shift+wheel scrolls
sideways), or by dragging empty space.

## Groups

A group is a named set of devices, drawn as a dashed frame with its name on
the top edge. Devices directly in the same group share a row.

```text
group fab1 s1 s2 s3        create fab1 (or add to it)
group fab(1) s4 s5         create the next free fabN
fab1                       select it: arrow keys / dragging move the whole group
ungroup fab1 s3            take s3 out of fab1
ungroup fab1               dissolve the group (the devices stay)
groups                     show every group as a tree
```

**Groups nest.** A group can contain other groups:

```text
group dc1 fab1 rfab1       dc1 contains two groups
group site dc1 fab2
```

- Something can belong to only one group at a time, so adding it to a new
  group moves it. A group can't end up inside itself.
- A parent group doesn't force its subgroups onto one row. Each subgroup keeps
  its own row, and the parent keeps its members together within every row.
- Frames sit inside each other, and the layout pushes apart sibling frames
  that would overlap.
- Dissolving a nested group moves its members up into its parent.
- Clicking an inner frame selects the inner group. Selecting a group moves
  everything inside it.

### Cloning a group

```text
clone office1              copy it; names are auto-numbered (office1 → office2, or the next free)
clone rfab1 rfab9          name the copy yourself (nested groups are still auto-numbered)
clone rfab1 rfab(1)        the next free rfabN
```

`clone` copies every device in the group, nested groups included, with the
same kinds and port counts. Every cable with **both ends inside** the group is
copied on the same ports with the same role. Cables to the outside are not
copied (the console says how many were left behind), so you cable the copy in
yourself.

- The group tree is recreated, and the copy joins the original's parent group.
  HA clusters stay clusters with the same split policy.
- STP and DHCP-server settings come along. Interfaces keep their mode but not
  their address: DHCP and ad-hoc re-address the copies. Static addresses would
  clash, so they become DHCP (computers) or automatic (routers).
- Copies start healthy, even if parts of the original are failed.
- With auto-arrange on, the copy is laid out with everything else. Otherwise
  it's placed just to the right of the original, and the new group is selected
  so the arrow keys move it.

A copied switch loop with STP off is a loop too: it storms on its own,
without affecting the original.

### Copying a group's outside cabling

```text
ports copy link1(rfab1, rfab2) link2(rfab2, rfab3)
copy link1(rfab1, rfab2) link2(rfab2, rfab3)
```

`ports copy` looks at the cables `link1` makes to `rfab1` and `rfab2`, and
gives the existing group `link2` the same pattern onto `rfab2` and `rfab3`.
Devices are matched by position in each group, so `link1 [s8 s12]` ↔
`link2 [s24 s25]`, `rfab1 [r1 r2]` → `rfab2 [r3 r4]`, and so on. Each pair of
groups must be the same size and the same kinds.

- **Only the outside cables to the listed groups are copied.** No devices are
  created, `link1`'s internal cables aren't copied, and cables to anything not
  listed are ignored.
- **Ports:** each new cable uses the same port numbers as the one it copies
  where they're free, else the first free port. The console lists any that
  moved.
- Running it again adds nothing: cables `link2` already has are counted.
- Every device must have room. This is checked first, so a copy that doesn't
  fit changes nothing, and the error says which device needs more ports.

`copy group(…) new(…)` (no `ports`) does the same, but first creates the new
group as a clone, with its internal cables. `new` can be auto-numbered:
`copy link1(rfab1, rfab2) link(1)(rfab2, rfab3)`.

## Renumbering (BFS / DFS)

Ids normally reflect the order you added things, with gaps left behind by
deletions. `renumber` gives each kind fresh numbers, 1..n, in the order a
breadth-first or depth-first walk of the cabling reaches them:

```text
renumber bfs               walk outward from the root, level by level
renumber dfs r2            go as deep as possible first, starting at r2
renumber auto dfs          keep ids in DFS order after every change
renumber off               stop; ids stay as they are
renumber                   show the current setting
```

- **The root** defaults to the first gateway, or else the first router,
  switch, then computer. In auto mode the root is remembered as a device, so
  it can be renamed itself; if it's deleted, the default is used.
- **Neighbours are taken in port order**, so `p1` is explored before `p2`.
  The walk follows the physical cabling: every cable counts (failed and
  control cables too), and the walk passes through computers like any other
  device. Devices the root can't reach are numbered afterwards, starting from
  the next one in the old order.
- **Everything follows the rename**: cables, group membership, addresses and
  DHCP settings stay with their device. Switch MAC tables are cleared, because
  MACs are built from device ids. The next new device of each kind gets n+1.
- The console lists what changed, for example
  `renumbered (DFS): s3→s1 c6→c1 …`.

With the basic demo, BFS from `g1` reaches `r1`, `r2`, `s1`, `s2`, `s3`, then
the computers. DFS dives `r1 → r2 → s3 → c6…` before coming back for `s1`, so
the old `s3` becomes `s1`.

## Addressing

A ByteSim address is a pair, **(network id, 8-bit host)**, written `(2,14)`.
There are no subnet masks or dotted decimals. A network id names a broadcast
domain: everything that can reach everything else through switches alone. One
network can span many switches. Ids are handed out from a single counter, so
two separate networks never clash.

Every port also has a **MAC address**: the device id plus a port letter. `R1A`
is port 1 of r1, and after `Z` come `AA`, `AB`… MACs identify the physical
port; the (network, host) pair is its logical address.

### How addresses are assigned

- **Routers and gateways** address themselves on every network they're cabled
  to. The first router on a network takes host 1, the next takes 2, and so on.
  If a router plugs into a segment that already has a network (say, two
  ad-hoc computers), it joins that network instead of opening a new one. Two
  routers cabled directly share a small network of their own.
- **Computers use DHCP** by default. Cabling a computer starts an exchange you
  can watch: a yellow DISCOVER spreads to every interface on the segment, then
  a green OFFER/ACK comes back from a router. Leases start at host 10, and the
  gateway is the router's host number.
- **Ad-hoc:** if no DHCP server answers, the computer takes an unused host on
  its local network, or opens a new network if it's the first one there. A
  pair of computers cabled directly works this way. When a router appears
  later, ad-hoc hosts switch to DHCP and keep their host number.
- Unplugging a computer releases its address. If a DHCP server is switched off
  or becomes unreachable, clients keep their existing leases.

```text
ip                         table of every interface: MAC, network, host, mode, gateway
ip c1                      one device
ip c5 (2,50) 1             static address (2,50) with gateway host 1
ip c5 dhcp | adhoc | none  change how c5:nic1 gets its address
dhcp c4 | dhcp all         run DHCP (animated)
release c7                 give up the lease (and stop asking)
adhoc c1                   take a free host without asking a server
dhcpd r1 off | on          switch a router's DHCP server off or on
```

Addresses show under each computer: grey for DHCP, yellow for ad-hoc, cyan
for static. Press `v` to hide or show them.

### Routing

- A computer delivers directly to anything on its own network. Anything else
  goes to its gateway.
- Routers forward between networks, choosing the path with the fewest cable
  hops. Two routers are neighbours when they share a network.
- Only gateways reach `internet`.
- **Equal-cost paths (ECMP):** when several paths are equally short, the
  router picks one by hashing the flow, meaning its source and destination.
  One conversation always takes the same path (so its packets stay in order),
  while different conversations spread across all the equal paths. In an
  active-active cluster, both routers share the load. The choice is stable
  from run to run, so seeded traffic stays reproducible.
- When something can't be reached, ByteSim says why: no address, no gateway,
  no route, or a gateway that isn't reachable.

Switches learn MAC addresses from the traffic that crosses them. Select a
switch to see its MAC table. Removing a cable or a device clears all the
tables.

## Sending traffic

```text
ping c1 c8                 echo request (red) and reply (green)
ping c4 (1,1)              destinations can be addresses
ping c1 internet           via the nearest gateway
unicast c2 c7              one-way packet (orange)
mcast c1 c4 c6 c8          one packet, copied where the paths split (magenta)
mcast c1 pcs all           receivers can be group names or `all` (every computer)
```

Each send logs the address path, an ARP line for every hop (`ARP (3,1) is-at
R1C`) and the TTL. Multicast also reports where copies were made and how many
cable crossings it took, compared with sending a separate unicast to each
receiver. A device blinks when a packet arrives, and the cables in use stay
highlighted until the packet is delivered.

## Random traffic

```text
traffic on                  2 packets a second between random computers
traffic on 10 seed=42       10 a second, the same flows every time you use seed 42
traffic on 5 hosts=office1  only computers in a group send and receive
traffic on mix=unicast      only some kinds (unicast, internet, broadcast)
traffic                     statistics
traffic rate 20  ·  traffic reset  ·  traffic off
```

Computers send random packets at a steady rate. By default about three in four
go to another computer, some go to the internet through a gateway, and a few
are broadcasts. Every packet follows the same rules as `ping`: addresses and
gateways (or plain cabling in graph mode), STP, failures, clusters. Switches
learn MACs from it. A broadcast or ARP into a live loop sets off a storm, as it
would in a real network.

- Traffic packets have their own colours: blue for unicast, pink for internet,
  pale yellow for broadcasts.
- Cables are shaded by how busy they've been in the last few seconds, from
  light to heavy, so bottlenecks show up.
- `traffic` reports:
  - packets sent, delivered, in flight, and dropped, with the reason
    (unreachable, no address, storm, device down);
  - the mix of traffic kinds;
  - the busiest cables and the devices that forwarded the most;
  - the top talkers.

  The inspector shows a running total, and the per-device counters for the
  selected device.

Try `demo ha`, `traffic on 8`, then `fail r1` and watch the traffic move to r2.
Or `demo loop`, `traffic on`: the first broadcast storms. Traffic isn't
recorded for undo, and it pauses while a script runs.

## Loops, spanning tree and broadcast storms

Switches forward a broadcast out of every port except the one it came in on.
In a tree that's fine: the frame reaches everyone once and dies out. In a
loop it comes back round and gets copied again, forever, because layer-2
frames have no TTL. That is a **broadcast storm**.

**Switches run spanning tree (STP) by default.** In each area of cabled-
together switches, STP elects a **root bridge** (the lowest bridge id:
priority, then switch number). It keeps one shortest path from every switch to
the root and **blocks** one end of every other switch-to-switch cable. Blocked
cables are drawn dashed, and the blocked port shows `⊘`. Switches with STP off
can't block anything, but they pass STP frames straight through, so a single
STP switch can still break a loop of plain switches. A loop made *only* of
non-STP switches can't be broken: it stays live.

```text
stp                          every switched area: root bridge, blocked ports, live loops, storms
stp off  |  stp on           for every switch
stp s4 s5 off                for some switches
stp priority s7 4096         lower priority wins the root election (default 32768)
broadcast c1                 flood one frame from c1 (or c1:2, r1:p6…)
```

**What sets off a storm:** a `broadcast`, a DHCP DISCOVER, or the ARP request
that starts every ping, unicast or multicast hop, sent into a live loop. The
console shows:

- the number of frames in flight at each hop. A plain ring just circles; a
  meshed loop multiplies (`1 → 3 → 9 → 12 → 18 → 30 → …`);
- **MAC flapping**: the sender's MAC arriving on different ports of the same
  switch, which is also shown in that switch's inspector;
- which switches are now saturated.

While a storm lasts, every cable touching those switches is drawn red with
dots running round the loop, and the switches pulse. Nothing else gets
through: pings, DHCP and broadcasts fail with
`— broadcast storm on s4 s5 s6 s7 is saturating the links`.

**A storm ends when its loop is broken:** turn STP on (`stp on`, or for one
switch), fail or unplug a cable in the loop, or delete a switch. ByteSim then
logs `broadcast storm over`.

**Link aggregation:** to use parallel cables between two switches instead
of letting STP block all but one, bundle them:

```text
lag s8 s12          every data cable between s8 and s12 now acts as one link
lags                list bundles and how many members are carrying traffic
unlag s8 s12        separate cables again
```

STP treats the bundle as one link and blocks none of its members. Loop
detection doesn't count it as a loop, and a broadcast crosses on one member
instead of circling. Unicast flows are spread across the members by the same
flow hash as ECMP. If a member fails, the others carry on. Bundled cables are
drawn in steel blue, and the inspector marks their ports `≡ lag`. Only
switch↔switch pairs can be bundled: between routers, parallel cables are
already separate routed links, and ECMP spreads flows across them.

**Failover:** failing a forwarding cable makes STP unblock a backup port
(`STP s7:p2 blocking → forwarding`), and traffic reroutes through it.

`bytesim --demo loop` has three switches in a triangle with STP off:

```text
broadcast c1        storm
stp on              s3:p1 blocks; the storm is over
fail s1:2           the blocked port takes over
ping c1 c2          c1 → s1 → s3 → s2 → c2
```

STP here converges instantly (real STP takes 30–50 seconds), and a storm is
declared once frames are still alive after more hops than there are cables.

## Cable roles and failures

Every cable has a role:

| Role | Drawn | Carries |
|---|---|---|
| `data` (default) | `─│` | everything |
| `control`, `control-backup` | `═║` | heartbeat and coordination only, never data |
| `fabric` | `━┃` | data directly between routers |

```text
r1:p5 r2:p5 role=control       cable with a role
role r1:4 fabric               change an existing cable's role
```

Router ports on control cables get no address.

Failures are injected by hand:

```text
fail r1            a device dies
fail r1:2          one cable
fail r1 r2         every cable between two devices
restore r1         bring something back · restore all
```

Failed cables turn red with a `✕` on them, and failed devices are struck
through. Routing, DHCP and broadcasts all route around failures. Addresses are
kept through a failure, so recovery is clean.

## HA router clusters

A cluster is a group of routers that act as one, sharing a **virtual gateway**
(host 254) on every network they serve.

```text
cluster rfab1 r1 r2                 create (r1 and r2 are grouped too)
cluster rfab1 policy=witness        change the split policy
cluster rfab1                       status and who owns each virtual gateway
clusters                            every cluster
uncluster rfab1                     back to a plain group
```

- DHCP clients of a clustered router get `(net,254)` as their gateway. The
  active member owns that address, and ownership moves per network: if r1
  loses its cable to fab2, r2 takes over `(2,254)` and r1 keeps the others.
- Members coordinate only over control cables. The cluster's frame shows its
  health, and every change is logged:

| State | Means |
|---|---|
| `healthy` | all members up, and losing any single control cable wouldn't split them |
| `no redundancy` | one more control failure splits the cluster |
| `failover` | a member is down; the others carry on |
| `SPLIT-BRAIN` | members are alive but can't coordinate, so each claims every virtual gateway (pings warn of an ARP conflict) |
| `partitioned` | split, but the policy let only one side stay active |
| `OUTAGE` | no side may stay active |

**Split policies:**

- `none` (default): every side stays active, so split-brain can happen.
- `quorum`: a side needs a strict majority. With two routers, any split or
  member failure is an outage. That is exactly why two-node clusters use a
  witness.
- `witness`: a majority wins, and a tie goes to the side holding the first
  surviving member. It fixes split-brain without failing over when a member
  actually dies.

Try `bytesim --demo ha`: r1 and r2 connect fab1 and fab2, with a control
cable, a control-backup cable and a fabric cable between them. Then:

```text
ping c1 c6
fail r1:5                   no redundancy
fail r1:6                   split-brain
cluster rfab1 policy=witness
restore all
fail r1:2                   r1 loses fab2: traffic crosses the fabric link
```

## Routed cores vs shared fabrics

Connecting several sites takes a choice between two designs. They look
alike on screen but behave very differently.

**A shared switching fabric.** Every site's routers plug into the same
switches. A switched area is *one* network, so every site router on it is a
direct neighbour of every other. Site 1 can send to site 2 across the switches
without ever touching the router you meant to be "the core". The core is
optional: traffic takes whichever path is shortest. One broadcast domain also
spans every site, so a loop or a storm there hits all of them.

**A routed core.** Each site router gets its own cable to each core router.
Every such cable is a separate little network, so no two sites share one. To
get from one site to another, a packet *must* be routed by a core router,
because nothing else connects them. Traffic within a site never needs the
core.

Make the core redundant with an HA cluster in active-passive mode:

```text
cluster core r1 r8 policy=witness mode=active-passive
r1:15 r8:15 role=control
r1:16 r8:16 role=control-backup
```

- **`mode=active-passive`**: only the active member (the first one alive in the
  winning partition) forwards traffic. The other stands by, and takes over the
  moment the active one fails or loses the election. The cluster's inspector
  says who's forwarding and who's on standby.
- **`mode=active-active`** (the default): every active member forwards.
  Routers simply pick the shortest path.

The split policy still decides what happens when the control links fail:
- `witness`: one side keeps forwarding.
- `quorum`: with two routers, a split means *neither* side forwards, so the
  sites are cut off from each other.
- `none`: both sides forward (split-brain).

`bytesim --demo core` is a three-site network built this way. Try `ping c1 c3`
(it goes through `r1`), `ping c1 c2` (it stays in the site), `fail r1` (the
standby `r2` takes over), `fail r3:3` (that site's other router carries the
traffic), and `traffic on 6` (the active core router's cables get busy).

## Graph mode

```text
mode graph        pure connectivity: shortest cable path, addresses ignored
mode addr         back to addressing (every address is recomputed from the cabling)
```

In graph mode, ping, unicast and multicast take the shortest cable path.
Computers still never forward traffic for others, and failures, control
cables, STP blocking and storms still apply (only `broadcast` sets off a storm
here, since there is no ARP). Address labels are hidden, automatic DHCP is off, and
switches don't learn MACs.

## Undo

```text
undo            take back the last change (u on the canvas)
undo 5          take back the last five
redo            put back what you undid (U on the canvas)
history         list the changes you can undo
```

Every command that changes the network is one step, up to 100 of them. That
includes `clear`, `load`, `demo`, `dhcp`, and a whole `run` script (even one
that stopped at an error halfway). A burst of moves with the arrow keys, or one
drag with the mouse, is one step too.

Commands that only look don't add steps: `list`, `ip`, `stp`, `help`. Nor do
pings that only teach switches MAC addresses, though a storm a ping sets off
does count. Making a new change after undoing clears what you could redo.

The history lasts for the session; it isn't saved with the network.

## Saving and loading

```text
save mynet.json      (default file: bytesim.json)
load mynet.json
export mynet.yaml    declarative topology (add `ports` to pin every cable)
load mynet.yaml      builds and auto-arranges it
```

There are two formats:

- **Save files** (JSON) are complete snapshots: positions, addresses and
  failures included.
- **Topology files** (YAML) describe the architecture with templates and link
  macros (`mesh`, `zip`, `connect`, `parallel`, `chain`, `ring`). They're meant
  to be written by hand or generated. Runtime state is left out. See
  [topology-format.md](topology-format.md).

`load` tells the two formats apart by their contents.

A third option is `export script`, which writes the console commands that rebuild
the network, reusing repeated parts with `clone` and `copy`. Replay one with
`clear` then `run <file>`. See [scripts.md](scripts.md).

`bytesim mynet.json` opens a file at startup. A loaded network keeps its own
positions, so auto-arrange starts off. The format is described in
[file-format.md](file-format.md).

## Command reference

`key=value` tokens can appear anywhere in a command line and become options
(`role=`, `policy=`). Typing a device id on its own selects it, two ids cable
them, and a group name on its own selects the group.

| Command | Aliases | Does |
|---|---|---|
| `add <g\|r\|s\|c> [ports] [count]` | `new`, `+` | add devices |
| `<id> <id>` / `link a b [role=]` | `connect`, `ln`, `cable` | cable two devices |
| `unlink <id>[:port] [id]` | `disconnect`, `cut` | remove cables |
| `del <id> …` | `rm`, `delete`, `remove` | delete devices |
| `ports <id> <n>` | `nics`, `resize` | change the port count |
| `role <cable> <role>` | | change a cable's role |
| `fail …` / `restore … \| all` | `down` / `up`, `repair` | inject or clear failures |
| `group <name> [members]` | | create, extend or select a group |
| `ungroup <name> [members]` | | take members out, or dissolve |
| `groups` | | the group tree |
| `clone <group> [new-name]` | `copy`, `dup` | copy a group and its internal cables |
| `copy <group>(old, …) <new>(new, …)` | | clone it, cabled to the new endpoints like the original is to the old |
| `ports copy <group>(old, …) <group>(new, …)` | | cable an existing group the way another one is cabled |
| `cluster <name> [routers] [policy=] [mode=]` | | create, configure or inspect an HA cluster (mode: active-active / active-passive) |
| `clusters`, `uncluster <name>` | | list clusters / turn one back into a group |
| `ip [id[:port]] [addr [gw] \| dhcp \| adhoc \| none]` | | show or set addresses |
| `dhcp`, `release`, `adhoc <id[:nic]\|all>` | | client address control |
| `dhcpd <router> [on\|off]` | | router DHCP server |
| `ping <src> <dst>` | `trace`, `traceroute` | echo and reply |
| `unicast <src> <dst>` | `ucast`, `send` | one-way packet |
| `multicast <src> <dst…>` | `mcast` | one-to-many packet |
| `mode graph\|addr` | | graph or address mode |
| `traffic on [rate] [seed=] [hosts=] [mix=]`, `traffic [off\|rate n\|reset]` | | random background traffic and statistics |
| `broadcast <id[:port]>` | `bcast`, `flood` | flood one frame (storms in a live loop) |
| `lag <switch> <switch>`, `unlag …`, `lags` | | bundle parallel cables into one logical link |
| `stp [on\|off]`, `stp <switch…> on\|off`, `stp priority <switch> <n>` | | spanning tree |
| `renumber [auto] bfs\|dfs [root]`, `renumber off` | `rename` | renumber ids in traversal order |
| `auto` | `arrange`, `layout` | re-arrange and keep arranging |
| `list` | `ls` | devices, cables, groups |
| `demo [basic\|ha\|loop\|core]` | | load a sample network |
| `save [file]`, `load [file]` | `open` | save files; `load` also reads topologies |
| `export <file.yaml> [ports]` | | write a declarative topology |
| `export script [readable\|shortest\|explicit] [file]` | | the commands that rebuild the network (verified) |
| `run <file>` | `source` | run a script of commands |
| `clear` | | empty the network |
| `undo [n]`, `redo [n]`, `history` | | step back and forward through changes |
| `help` | `?`, `h` | command summary |
| `quit` | `exit`, `q` | leave (`Ctrl+Q` also works) |

## Keys and mouse

| On the canvas | |
|---|---|
| arrows / Shift+arrows | move the selected device or group (1 or 4 cells) |
| `Tab` / Shift+`Tab` | next / previous device |
| `a` | auto-arrange |
| `u` / `U` | undo / redo |
| `x`, `Delete` | delete the selected device, or dissolve the selected group |
| `v` | show or hide addresses |
| `h` `j` `k` `l` | pan |
| `Enter`, `:`, `/`, `i` | go to the command line |
| `Esc` | clear the selection |
| click / drag a box | select / move a device |
| click / drag a frame | select / move a group |
| drag empty space, wheel | pan |

On the command line, ↑/↓ recall earlier commands and `Esc` returns to the
canvas.

## What is simplified

ByteSim models the behaviours above and deliberately skips the rest:

- Leases never expire, and HA coordination is instant (no heartbeat timers).
- Every router automatically knows every network it can reach; there's no
  real routing protocol.
- Routers forward multicast as if it were unicast to each receiver.
- In split-brain, which router a host reaches is fixed by cabling order, not
  by ARP races.
- STP converges instantly, and every hop's ARP is a fresh broadcast (there's no
  ARP cache).
- Ping times are random. Hop counts and TTLs are real.
