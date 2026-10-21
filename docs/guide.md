# ByteSim user guide

- [The screen](#the-screen)
- [Console logging](#console-logging)
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

## Console logging

Use `tee` to copy new console output to a file while keeping it visible:

```text
tee traffic.log              start appending to a file
detail on                   enable detailed output for the session
mping all all               capture every reply and ARP line
tee                         show the active file (or off)
tee off                     stop file logging
```

Files contain plain UTF-8 text without Rich colours or console wrapping. Each
message is flushed so another terminal can follow it with `tail -f traffic.log`.
Existing content is retained. Capture includes later packet replies and errors;
it starts when enabled and does not copy earlier console history. `detail on`
still controls multicast verbosity in both the console and the file.

Relative paths use ByteSim's working directory, `~` expands to your home
directory, and paths with spaces may be quoted. `tee <another-file>` switches
files. A failed open leaves the previous log active; a failed write disables
the tee and reports an error in the console.

To capture from startup, including the welcome message:

```sh
bytesim mynet.json --log-file traffic.log
```

The tee stays active across network loads, clears and undo/redo. It closes on
exit and is session state, independent of saved networks.

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

With **auto-arrange** on (the default), every change re-lays out the network
using the selected mode. Choose one at the console:

```text
layout layered    rows by device kind (the default)
layout tree       rows by physical cable distance from a root
layout circular   devices and groups around a ring
layout grid       compact rows that wrap to the canvas width
layout compact    tighter spacing and height-based packing of group blocks
layout compact-layered   device-kind layers with tighter spacing
layout list       show current settings and available choices
```

`layout` accepts a layout, an orientation, or both, in either order:

```text
layout grid vertical       grid layout, filling columns before wrapping
layout vertical grid       the same settings, in the other order
layout horizontal          change only the layout's direction
layout tree                change only the layout
layout auto                use the selected layout's default direction
```

Arguments are identified by membership in the layout and orientation choices;
at most one of each is allowed. Orientation changes the direction of the
selected algorithm; it preserves its levels, wrapping or ring. Groups inherit
that direction unless given an override. `auto` and `arrange` accept the same arguments. With no
argument, all three commands re-arrange using the current settings and turn
auto-arrange on.

**Layered** keeps the original arrangement:

- Gateways go on the top row, then routers, switches, and computers at the
  bottom. A chain of devices of the same kind (`r1`–`r2`, or one switch
  feeding another) gets an extra row.
- Devices in each row are ordered to reduce crossing cables, then shifted to
  line up with what they connect to.
- Members of a group stay together, as described below.

**Tree** uses breadth-first cable distance instead of device kind. The root
is the first gateway, or the first router, switch, then computer. Disconnected
components get their own roots in that order. Every physical cable counts, including
failed and control cables, so injecting a failure doesn't reshape the view.

**Circular** places the outermost devices and groups clockwise around an
ellipse. Groups move as complete blocks and use the circular layout inside
their frames too. **Grid** packs devices and group blocks into rows or columns,
wrapping to the canvas width or height; a block larger than that dimension
gets a row or column of its own.

**Compact** uses smaller gaps and packs blocks by height for rows or width for
columns, filling remaining space before opening another row or column. This reduces
wasted space when groups have different sizes. It applies inside nested groups too.
Compact packing may change the visual order of blocks
to fit them more closely. Device boxes and group frames retain their full size,
with space for address labels and cables.

**Compact-layered** keeps the layered graph ordering, including extra levels
for chains of the same device kind, with smaller gaps between blocks and
levels. Packing stays within each layer, so gateways, routers, switches and
computers retain their hierarchy. Nested groups use the same arrangement.
Use `layout compact-layered horizontal` or `layout vertical compact-layered`
to choose the flow direction.

**Horizontal / vertical** controls flow without flattening the network:

- Layered, compact-layered and tree: levels run left to right or top to bottom;
  branches remain.
- Grid and compact: fill rows or columns, then wrap to the next one.
- Circular: orient the ellipse to the canvas width or height; keep the ring.

`auto` uses top-to-bottom flow for layered, compact-layered and tree, and rows or a width-based
ellipse for the other modes. Resizing the terminal updates the available space.

Every mode keeps nested groups together and makes room for their frames.
Large networks may extend beyond the canvas; pan to see the rest. Layouts
are deterministic and only change positions, never connectivity or routing.
The selected mode is saved with JSON snapshots and restored by undo/redo.
YAML topologies and exported construction scripts leave display settings out,
along with positions.

### Group orientation

Choose the direction inside a group independently of the overall layout:

```text
group fab1 layout=vertical       use the layout's vertical flow inside fab1
group fab1 layout=horizontal     use its horizontal flow inside fab1
group fab1 layout=auto           follow the overall orientation
group office c3 c1 c2 layout=vertical   create a group with vertical flow
```

Each group uses the selected layout algorithm, so a tree keeps its branches,
a grid wraps, and a circular group keeps its ring. Nested groups move as
complete blocks and can have their own orientations. Member order breaks ties
when the layout has no other ordering rule. These options work in every layout
mode and re-enable auto-arrange. Orientation is kept in
JSON snapshots, undo/redo, and group clones; YAML and construction exports
leave it out.

To move things yourself, select a device and use the arrow keys (hold Shift to
move 4 cells at a time), or drag the box with the mouse. To select a device,
type its id on its own, press `Tab`, or click it. The first manual move turns
auto-arrange off. Press `a` or type `auto` to turn it back on using the selected
mode.

Pan the view with `h` `j` `k` `l`, the mouse wheel (Shift+wheel scrolls
sideways), or by dragging empty space.

## Groups

A group is a named set of devices, drawn as a dashed frame with its name on
the top edge. Members stay together and their arrangement follows the selected
layout and orientation.

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
- A parent group keeps its subgroups together while each subgroup retains its
  own internal arrangement.
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
- Every router takes one off a packet's TTL, which starts at 64. The 64th
  router on the way drops it (time exceeded), so at most 63 routers can lie
  between two hosts.
- When something can't be reached, ByteSim says why: no address, no gateway,
  no route, or a gateway that isn't reachable.

Every hop starts with ARP, and switches forward by what that teaches them:

1. The ARP request floods the sender's broadcast domain, and every switch it
   crosses learns the sender's MAC on the port it came in by.
2. The reply travels back by those entries, and the switches on its way learn
   the answering interface's MAC.
3. The frame follows the switches' entries. A switch that doesn't know the
   destination floods it; an entry for a cable that can't carry data counts
   as forgotten. A wrong entry would misdeliver or drop the frame, but the ARP
   reply has just corrected it.

ARP for an address nobody has is still sent: the host asks on its own network,
or the router on that network asks, and the broadcast floods (and can set off a
storm) before the send fails with no reply. Select a switch to see its MAC
table. Removing a cable or a device clears all the tables.

## Sending traffic

```text
ping c1 c8                 echo request (red) and reply (green)
ping c1:2 c8               send from c1's second NIC (computers only)
ping c4 (1,1)              destinations can be addresses
ping c1 internet           via the nearest gateway
mping c1 c4 c6 c8          multicast request (magenta), individual replies (green)
mping all c1               every other computer pings c1
mping all all              every computer pings every other computer
mping all all *            every ping-capable device pings every other one
mping all c1 *             routers, gateways and computers each ping c1
mping c1 r1:p2             probe a specific router interface
detail on                  include every reply and ARP in subsequent commands
ping c1 all                multicast ping to every other computer
ping c1 office             multicast ping to members of a group
unicast c2 c7              one-way packet (orange)
mcast c1 c4 c6 c8          one packet, copied where the paths split (magenta)
mcast c1 pcs all           receivers can be group names or `all` (every computer)
```

An echo reply is a packet of its own: the receiver routes it back to the
interface that asked, so a ping fails with `no reply` when the receiver has no
way back (no gateway, say), even though the request arrived. Replies through an
overlay retrace the request's tunnel. The reported TTL is the reply's.

Detailed sends log the address path, an ARP line for every hop (`ARP (3,1) is-at
R1C`) and the TTL. Multicast also reports where copies were made and how many
cable crossings it took, compared with sending a separate unicast to each
receiver. A device blinks when a packet arrives, and the cables in use stay
highlighted until the packet is delivered.

Packet animation scales with path length so large layouts stay responsive.
The animation targets 60 fps. At normal speed, the longest active one-way path
takes about 1.5 seconds. Multicast requests move together until their paths split.

```text
speed slow        half speed, for following packets closely
speed normal      default playback speed (1x)
speed fast        three times normal speed
speed 0.75        choose your own multiplier (0.1–10; `speed 2x` also works)
speed             show the current speed
```

Changes apply to packets already in flight. Speed is a session preference,
retained when loading or clearing a network. It controls animation timing;
`traffic rate` still controls how many background packets are generated.

### Pause and simultaneous actions

Use `pause` to freeze packets already in flight and stop background traffic.
Packet commands entered while paused wait in a queue:

```text
pause
ping c1 c6
mping c2 c3 c4
unicast c5 c6
queue                 inspect the pending commands
resume                launch all queued requests on the next animation tick
```

`play` and `unpause` are aliases for `resume`. `queue clear` discards queued
commands while leaving playback paused. The inspector shows whether playback
is paused and how many actions are queued.

Ping, multicast ping, unicast, multicast, broadcast and explicit DHCP requests
are queued, including their aliases. Editing, inspection and `speed` still work
while paused. Routes are calculated when the batch starts, using the current
topology. If a queued action fails, its error is reported and the remaining
actions still start. Existing packets resume from their frozen positions;
background traffic resumes without a catch-up burst.

Replacing the network (`clear`, `load`, `demo`, or undo/redo) discards queued
actions so they cannot target a different network. Pause remains a session
setting. Queues and playback state are not saved in network snapshots.

`mping <src> <receivers…>` sends one multicast echo request and collects a
separate reply from each receiver. `ping` chooses this behavior when given
multiple destinations, a group, `all`, or `*`. You can also write
`mcast ping c1 c4 c6` (`multicast-ping` and `mcastping` are aliases for `mping`).
Nested groups expand to their members, duplicate targets are removed, and the
sender is excluded. `all` as a target means every other computer. `all` as a
source starts one multicast ping from each computer that has another receiver;
`mping all all` checks every ordered pair of distinct computers. Replies and
summaries identify their sender so concurrent results can be distinguished.

Append `*` after the destinations to expand `all` on both sides to every
ping-capable device: `mping all all *`. In address mode, eligible devices are
computers, routers and gateways with at least one assigned address. ByteSim's
switches have no management address and remain excluded. Unaddressed devices
are ineligible; configured devices that are down or unreachable still participate
and produce failures. Each device excludes itself, including its other interfaces.
Wildcards select each device once, so 60 eligible devices produce 3,540 ordered
pairs. In graph mode, every device is eligible, including switches and unaddressed
nodes, and pings test physical paths rather than IP reachability. The existing
two-argument forms `mping c1 *` and `mping * *` retain their computer-only selection.

A bare device target probes one of its addressed interfaces using the route with
the fewest cable hops; equal-length routes choose the lowest numbered destination
port. Use an interface such as `r1:p2` or an address such as `(3,1)` to select the
target explicitly. Multicast preserves that selection and deduplicates equivalent
address/port targets. With detailed output, each request shows its originating
interface/address and selected destination interface/address. A router's selected
address can be reached through another interface on that router; the destination
address and physical ingress port need not be the same.

Trace and detail are global session settings. For full request and return paths:

```text
trace on
detail off
mping c1 c6
mping all all *
```

Each receiver gets a `TRACE REQUEST` and a `TRACE REPLY`, identifying the sender
and target, addressed endpoints, and every physical cable hop, including switches.
For example, `r1:p2 -fabric#3-> r2:p1` means the selected cable is link 3 (its
one-based position in the network's cable list), carrying fabric traffic from
router 1 port 2 to router 2 port 1. Successive hops show each transit device's
ingress and egress ports, including the chosen LAG member. Endpoint addresses
are omitted in graph mode.

Request traces describe routes selected during preparation; reply traces appear
when requests arrive and their return routes are calculated. Failed route queries
produce `TRACE REQUEST FAILED` or `TRACE REPLY FAILED` with the reason. Traces
describe forwarding decisions; the reply summaries count completed deliveries.

`trace off` is the default. `trace` and `detail` are independent: use
`trace on` with `detail off` for full paths and per-sender summaries, or use
`detail on` for the existing ARP and reply diagnostics too. `detail auto` is the
default: small actions show details, while large multicast batches summarize.
`trace` and `detail` without arguments show their current values. Changes affect
subsequent output immediately, including preparation and replies already in flight;
queued actions use the settings active when resumed. The inspector displays both
settings. They persist across network loads, clears and undo/redo, independently
of saved networks. Tracing also applies to ordinary ping, unicast and one-way
multicast. `traceroute` and `tracert` remain ping aliases; `trace` controls output.

Startup flags set the same preferences:

```sh
bytesim mynet.json --trace --no-detail --log-file paths.log
```

Use `--detail` for full diagnostics, `--no-detail` for concise output, and
`--no-trace` to disable paths. Per-command `trace=` and `detail=` options are
rejected with guidance to use the global settings. Tracing works with groups, all
senders, the whole-network `*` modifier, aliases and paused queues. Use
`tee paths.log` to save the same traces as plain text.

Batches of 128 or more source/receiver pairs prepare routes in short slices
while the UI remains responsive. Requests wait at position zero until the whole
batch is ready, then leave together. Return-route calculations also yield between
small batches of arrivals. `pause` freezes preparation as well as playback;
`resume` continues it. Editing the network, layout or simulation mode cancels prepared and
in-flight packets so their paths cannot reference an older topology.

Large batches default to one reply/failure summary per sender instead of logging
every packet and ARP. All pairs still run, with real return routes and MAC
learning. Use `detail on` to show every reply, or `detail off` to summarize a
smaller batch. Summaries count every failure; summary mode shows at most five
individual request errors and five return-route errors per sender.

The request shares cables until its paths split, just like ordinary multicast.
Each receiver routes its own reply back to the sender's originating interface
when the request arrives, so a request can arrive even if the reply has no
return route. Detailed output reports each reply's hops, TTL and simulated time,
plus a summary such as `multicast ping: 2/3 replies, 1 failed`. Unreachable
receivers and failed return routes are reported individually in detailed mode; other receivers
still reply. This also works in graph mode, using physical paths. A single
device or internet destination with ordinary `ping` keeps its existing behavior.

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
priority, then switch number). Every port has a **path cost** (20000 by
default, as for a 1 Gb/s port), and a switch's root path cost is the sum of
the costs of the ports a frame from the root enters by. Each switch's **root
port** is its cheapest way to the root (ties go to the lower bridge id, then
port), and STP **blocks** one end of every other switch-to-switch cable: the
end farther from the root. Blocked
cables are drawn dashed, and the blocked port shows `⊘`. Switches with STP off
can't block anything, but they pass STP frames straight through, so a single
STP switch can still break a loop of plain switches. A loop made *only* of
non-STP switches can't be broken: it stays live.

```text
stp                          every switched area: root bridge, blocked ports, live loops, storms
stp off  |  stp on           for every switch
stp s4 s5 off                for some switches
stp priority s7 4096         lower priority wins the root election (default 32768)
stp cost s7:3 2000           a cheaper port is preferred toward the root (default 20000)
broadcast c1                 flood one frame from c1 (or c1:2, r1:p6…)
```

When the tree changes (a cable fails, a priority or cost changes), the switches
in that area flush their MAC tables, as a topology-change notice makes them.

**What sets off a storm:** a `broadcast`, a DHCP DISCOVER, a gratuitous ARP
from a computer whose link comes up, an ARP request, or a frame for a MAC no
switch knows (flooded), sent into a live loop. The
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

**Link aggregation:** to use parallel cables instead of letting STP block
all but one, bundle them into a single logical link:

```text
lag                 find aggregatable spans (two or more parallel data cables)
lag auto            bundle every one of them
lag s8 s12          bundle one span: switch↔switch, or switch↔router / gateway
lags                list bundles and how many members are carrying traffic
unlag s8 s12        separate cables again
```

When you cable a second parallel cable that could be bundled, ByteSim
suggests the `lag` command.

What changes once cables are bundled:
- **STP and loops:** the bundle counts as one link, so STP blocks none of its
  members, loop detection doesn't count it as a loop, and a broadcast crosses
  on one member instead of circling.
- **Load:** unicast flows are spread across the members by the same flow hash
  as ECMP. If a member fails, the others carry on.
- **A router or gateway end** treats the members as **one interface with one
  address** (on its lowest-numbered member port). Traffic to it is spread
  across all members, and it stays reachable if any member is up, even when
  the primary port's own cable fails.
- **Display:** bundled cables are drawn in steel blue, and the inspector
  marks their ports `≡ lag`.

Router↔router pairs can't be bundled: their parallel cables are separate
routed links, and ECMP already spreads flows over them. Computers can't be
bundled either.

**Multi-chassis aggregation (MLAG, like vPC):** a normal bundle needs all its
cables on the *same* pair of devices. To bundle a gateway's cables to several
switches, those switches must first act as one logical switch:

```text
s1 s2                  peer links: the domain's switches must be cabled together
s2 s3
mlag fab1              s1 s2 s3 (group fab1) now act as one switch
lag                    finds spans like  g1–fab1: 3 cables
lag auto               bundles them: lag g1 fab1, lag r2 fab1, …
mlags  ·  unmlag fab1
```

Once a domain is declared:
- **Detection and bundling:** a device with cables to two or more of the
  domain's switches can be bundled to the domain as a whole.
- **What the bundle gives you:** everything a normal bundle does. One interface
  and one address on a router or gateway, no STP blocking, flows spread across
  members. The interface stays up while any switch or member is.
- **Real vPC forwarding rules:** traffic leaves on the switch it's already on
  when that switch has a member. A frame that crossed a peer link is never sent
  out a bundle, so a broadcast can't loop back to the device that sent it.
- **Display:** peer links are drawn in purple and marked `⇋ peer` in the
  inspector. The domain's frame says `MLAG`.

`bytesim --demo mlag` stacks this seven tiers deep: a gateway, a clustered
router pair, then spine, aggregation, leaf and rack switches and computers in
two pods. Spine, aggregation and leaf are MLAG pairs, each rack switch is
dual-homed to its leaf pair, and every span between tiers is one bundle
(back-to-back MLAG, like double-sided vPC), so nothing is blocked.
Try `mlags`, `lags`, `ping c1 c8`, then fail a spine, aggregation or leaf
switch (`fail s1`, `fail s3`, `fail s7`) or a router (`fail r1`), and run
`mping all all *` again: every rack stays reachable. `fail s15` shows the
weak point that's left: each rack has a single rack switch, so that rack is
cut off (computers can't be bundled, so there is no dual-homed host).

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
  says who's forwarding and who's on standby. A host whose gateway is the
  standby's own address (not the virtual gateway) can reach the standby
  itself, but nothing beyond it.
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

## The internet, NAT and overlays

Gateways normally lead to an implicit internet. You can also build the internet
yourself, put sites behind NAT, and connect them with a mesh VPN that crosses it.

### The internet and NAT

```text
internet isp           the group isp (ISP gateways, routers, public servers) is the internet
internet               show it: which gateways face it, their WAN ports and NAT
internet off           back to an ordinary group
nat g4 off|on          NAT on a gateway that faces the internet (on by default)
nat g4 hard|easy       the kind of NAT (easy by default)
```

A gateway's **WAN ports** are its ports with the shortest cabled path to the
internet group. An uplink through a private router or through another NAT
counts. A LAN that happens to reach the internet through a second gateway
doesn't. Failures don't change which side is which.

A gateway with a WAN port runs **NAT**:
- **Going out:** traffic from its LAN leaves with the gateway's WAN address. The
  trace says `NAT at g3: (6,10) leaves as (2,2)`.
- **Coming in:** a packet arriving on a WAN port isn't forwarded onto the LAN
  unless it's a reply to something that went out. Otherwise it's dropped,
  with the reason: `dropped by g4's NAT: unsolicited inbound traffic …`.
- **Behind another NAT** (double NAT), each NAT translates in turn.

With an internet declared, `ping c1 internet` has to reach one of its gateways.
Random traffic counts NAT drops separately.

`bytesim --demo internet` has three sites behind NAT gateways (g3–g5) and an
ISP (g1, g2) with a public server, c7. `ping c1 c7` works, `ping c1 c3` is
dropped by site 2's NAT, and `nat g4 off` opens site 2.

### Overlays

An overlay is a mesh VPN, modeled on how Tailscale works, though not a fully
faithful implementation of it. Its members reach each other through every NAT
on addresses that don't depend on cabling.

```text
overlay ol c1 c3 c5 c7          make overlay ol (or add members); a group adds its devices
overlay ol c4 host=9            choose a member's overlay host number
overlay ol remove c3            take members out
overlay ol                      members, their real endpoints, NAT and (up to 10 members) every peer
overlay ol peers c3             how one member connects to each of the others
overlay off ol                  delete the overlay
ping c1 c3@ol                   to c3 over the overlay (or its overlay address: ping c1 (100,2))
ping c1 all@ol                  every other member, as a multicast ping
```

- **Addresses:** each overlay has its own network, 100 for the first, then
  101 and so on (100–109 are reserved for overlays). A member keeps its
  overlay host number until it leaves, and a new member takes the lowest free
  one. `net=` picks the network when you create an overlay.
- **Tunnels:** a packet to `c3@ol` travels as WireGuard between the two
  members' real interfaces, over the normal route. The log shows the inner
  packet (`PING (100,1) → (100,2)`) and the trace shows both.

### Direct, relayed and the coordination server

```text
overlay ol coord=c7             members register through c7
relay c8                         c8 runs a relay (relay off c8, relay to list)
```

- **Direct:** a pair connects directly by hole punching: each peer sends
  first, so each NAT treats the other's packets as replies. This works
  unless *both* ends are behind hard NAT.
- **Relayed:** when both ends are behind hard NAT, or there's no direct path
  at all, the traffic goes through the nearest relay that both can reach. Each peer keeps an
  outbound connection to the relay, so no NAT is in the way.
- **The coordination server:** a new member must be able to reach it to join.
  Packets never pass through it, so when it's down, existing tunnels keep
  working and only joining stops.

`bytesim --demo overlay` adds an overlay to the internet demo. Sites 2 and 3
have hard NAT, c7 coordinates and c8 relays:

| Try | What happens |
|---|---|
| `ping c1 c3@ol` | direct |
| `ping c3 c5@ol` | relayed via c8 |
| `fail c8` | only c3 ↔ c5 breaks |
| `fail c7`, then `overlay ol c2` | existing tunnels keep working; c2 can't join |

### Subnet routers and exit nodes

```text
overlay ol advertise c3         c3 carries members onto its own LAN (unadvertise to stop)
overlay ol exit=c7              members' internet and other non-local traffic leaves through c7
overlay ol lan=on               with an exit node, own-site traffic stays local
```

- **A subnet router** advertises the networks it's on. A member can then reach
  devices there that aren't in the overlay with a plain `ping c1 c4`: the
  packet tunnels to c3, which forwards it with its own address (source NAT).
- **An exit node** carries everything that isn't on the member's own networks.
  By default that includes other networks in the member's
  own site. They're then unreachable, because the exit node can't get back
  through the site's NAT. `lan=on` keeps them local.
- **Who can use them:** only members, and traffic on a member's own network
  never goes through the overlay. A subnet route wins over the exit node.

### Access rules

```text
acl ol                          list the rules
acl ol allow c1 c3              let c1 start traffic to c3
acl ol allow site1 internet     let site1's members use the exit node
acl ol remove 1                 remove rule 1
acl ol clear                    no rules: everything allowed (the default)
acl ol none                     rules on, nothing allowed
```

- **What a rule names:** a device, a group (its devices), `*` (every device)
  or `internet`. `*` doesn't include the internet: it has to be named.
- **Deny by default:** once an overlay has rules, only what a rule allows gets
  through. Emptying the list keeps denying; only `acl ol clear` allows
  everything again. Replies are always let back.
- **What rules cover:** member-to-member traffic, subnet destinations, and use
  of the exit node.

## Graph mode

```text
mode graph        pure connectivity: shortest cable path, addresses ignored
mode addr         back to addressing (every address is recomputed from the cabling)
```

In graph mode, ping, unicast and multicast take the shortest cable path.
Computers still never forward traffic for others, and failures, control
cables, STP blocking and storms still apply (only `broadcast` sets off a storm
here, since there is no ARP). Address labels are hidden, automatic DHCP is off, and
switches don't learn MACs. `ping c1 c3@ol` takes the cable path to c3, once
overlay membership and access rules allow it; NAT doesn't apply.

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
| `ports <id…> <n>` | `nics`, `resize` | change the port count of one or more devices |
| `mesh A B`, `zip A B`, `parallel a b n`, `chain x…`, `ring x…` | | cabling patterns; a name is a device, a group (its devices in order) or `a,b,c`; `role=` applies to all |
| `role <cable> <role>` | | change a cable's role |
| `fail …` / `restore … \| all` | `down` / `up`, `repair` | inject or clear failures |
| `group <name> [members] [layout=auto\|horizontal\|vertical]` | | create, extend or select a group; orient the selected layout within it |
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
| `ping <src> <dst…>` | `tracert`, `traceroute` | echo and reply; groups, `all` or several destinations use multicast |
| `mping <src\|all> <id\|addr\|group\|all> … [*]` | `mcastping`, `multicast-ping` | shared echo requests and replies; trailing `*` expands eligibility |
| `trace [on\|off]` | | global full-path output; no argument shows status |
| `detail [on\|off\|auto]` | | global diagnostic verbosity; auto adapts to batch size; no argument shows status |
| `unicast <src> <dst>` | `ucast`, `send` | one-way packet |
| `multicast <src> <dst…>` | `mcast` | one-to-many packet |
| `mode graph\|addr` | | graph or address mode |
| `traffic on [rate] [seed=] [hosts=] [mix=]`, `traffic [off\|rate n\|reset]` | | random background traffic and statistics |
| `speed [slow\|normal\|fast\|0.1..10]` | | set or show packet animation speed; normal is 1x |
| `pause`, `resume` | `play`, `unpause` (resume) | freeze playback and queue packet commands, then start them together |
| `queue [clear]` | | inspect queued actions or discard them |
| `tee [file\|off]` | | append console output to a text file, show status, or stop capture |
| `broadcast <id[:port]>` | `bcast`, `flood` | flood one frame (storms in a live loop) |
| `lag`, `lag auto`, `lag <switch\|domain> <switch\|router\|gateway\|domain>`, `unlag …`, `lags` | | find and bundle aggregatable spans |
| `mlag <group…>`, `unmlag <group>`, `mlags` | | make a group of switches act as one (needs peer links) |
| `stp [on\|off]`, `stp <switch…> on\|off`, `stp priority <switch> <n>`, `stp cost <switch:port> <n>` | | spanning tree |
| `renumber [auto] bfs\|dfs [root]`, `renumber off` | `rename` | renumber ids in traversal order |
| `layout [layered\|tree\|circular\|grid\|compact\|compact-layered] [auto\|horizontal\|vertical]`, `layout list` | `auto`, `arrange` | layout and/or flow direction, in either order; no argument reuses current settings; `list` shows choices |
| `list` | `ls` | devices, cables, groups |
| `internet [<group>\|off]` | | declare or show the public internet |
| `nat <gateway…> on\|off\|easy\|hard`, `nat` | | NAT on gateways that face the internet |
| `overlay [<name>]`, `overlay <name> <device\|group…> [host=]`, `overlay <name> remove …`, `overlay off <name>` | | create overlays, add or remove members, show status |
| `overlay <name> peers <member>`, `overlay <name> advertise\|unadvertise <member…>` | | one member's connections; subnet routers |
| `overlay <name> coord=<device>\|none exit=<member>\|none lan=on\|off net=<id>` | | coordination server, exit node, LAN access, network |
| `relay [off] <device…>`, `relay` | | relays for overlay pairs that can't go direct |
| `acl <overlay> [allow <from> <to>\|remove <n>\|clear\|none]` | | access rules |
| `demo [basic\|ha\|loop\|core\|mlag\|internet\|overlay]` | | load a sample network |
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
- Ping times are random. Hop counts and TTLs are real. `traceroute` is ping
  under another name; it doesn't send probes with rising TTLs.
- MAC entries never age out, and only a dead cable makes a switch forget one
  (or removing a cable, which clears every table).
- NAT decides only what gets through. Addresses stay globally unique (there are
  no overlapping private ranges), and there are no port forwards.
- Hole punching works unless both ends are behind hard NAT; real mesh VPNs
  sometimes get through even then (port mapping, port guessing).
- An overlay has one exit node, used by every member, and access rules have no
  ports, protocols or tags.
