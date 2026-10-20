# Architecture

ByteSim has two halves: a pure-Python **model** that knows nothing about the
screen, and a **Textual UI** that draws the model and turns typed commands
into model calls.

```
src/bytesim/
├── __init__.py          version, re-exports Network & friends
├── __main__.py          python -m bytesim
├── cli.py               argument parsing, starts the app
├── constants.py         device kinds, sizes, token regexes, address constants
├── demos.py             ready-made networks (basic, ha, loop)
├── topology.py          the YAML topology format: load_topology(), dump_topology()
├── scripting.py         ScriptRunner / replay(): run command scripts headlessly
├── synthesis.py         export script: plan, find reuse, render, verify by replay
├── model/               ── no UI imports below this line ──
│   ├── types.py         Device, Link, Iface, NetError, fmt()
│   ├── base.py          NetworkBase: the state + basic lookups
│   ├── groups.py        nestable groups, frame bounds
│   ├── cloning.py       clone_group(): devices, nested groups, internal cables
│   ├── layer2.py        usable cables, broadcast domains, frame paths, MAC learning, graph-mode BFS
│   ├── addressing.py    router autoconfig, DHCP, ad-hoc, reconcile()
│   ├── routing.py       host → gateway → cheapest router path
│   ├── ha.py            clusters: partitions, policies, virtual-gateway owners
│   ├── layout.py        layered, tree, circular, grid and compact variants
│   ├── naming.py        BFS/DFS traversal, renumber()
│   ├── stp.py           spanning tree, loop detection, flooding, storms
│   ├── equivalence.py   canonical form of a network; differences()
│   ├── traffic.py       random traffic: generator (seeded), cable load, statistics
│   └── network.py       Network = all mixins + editing + JSON
└── tui/
    ├── app.py           ByteSimApp: layout, dispatch(), changed()
    ├── canvas.py        NetCanvas: drawing, mouse/keys, packet animation
    ├── cables.py        where cables run, line glyphs
    ├── inspector.py     side-panel text
    ├── command_input.py the command line with history
    ├── help.py          `help` text
    ├── theme.py         colours
    └── commands/
        ├── registry.py  @command decorator, collect()
        ├── base.py      CommandHost: what handlers may use
        ├── topology.py  add, link, del, ports, role, fail, files, mode…
        ├── groups.py    group, ungroup, clone, cluster…
        ├── addressing.py ip, dhcp, release, adhoc, dhcpd (+ animated DHCP)
        ├── naming.py    renumber (one-shot and auto mode)
        ├── switching.py broadcast, stp, storm reporting + flood animation
        ├── scripting.py export script, run
        ├── history.py   undo / redo (snapshot per changing command)
        ├── interpreter.py tokens + dispatch (shared by the app and ScriptRunner)
        └── traffic.py   ping, multicast ping, unicast, multicast
```

## The model

`Network` (in `model/network.py`) is assembled from mixins that each own one
concern and share the state defined in `NetworkBase`:

| State | Meaning |
|---|---|
| `devices: dict[str, Device]` | `"r1"` → kind, number, port count, position, up/down |
| `links: list[Link]` | cables: two (device, port) ends, role, up/down |
| `groups: dict[str, list[str]]` | group → members (device ids or group names) |
| `ifaces: dict[str, Iface]` | `"c1:1"` → mode, network, host, gateway, how it was obtained |
| `clusters: dict[str, str]` | HA cluster (a group name) → split policy |
| `dhcp_off`, `mac_tables`, `counters`, `notes` | the rest |

The model's core questions are answered by a few functions:

- `usable(link)`: can this cable carry data? Not if it's down, a control
  cable, either end is dead, STP is blocking either end, or a broadcast storm
  has saturated it. Everything that forwards goes through this.
- `segment(dev, port)`: the broadcast domain seen from one interface (every
  other interface reachable through switches alone).
- `l2path(src_iface, dst_iface)`: the frame path between two interfaces.
- `route(src, dst_iface)`: a list of **legs**, each one layer-2 hop between
  addressed interfaces: `(from_iface, to_iface, frame_path)`.
- `ha_state(cluster)`: partitions, active sides, virtual-gateway owners, and
  a health level.
- `stp_update()`: recomputes blocked ports (union-find over the switch graph,
  nearest-the-root first) and ends storms whose loop is gone.
- `flood(iface)`: follows one broadcast, generation by generation. Copies are
  counted per (cable, direction), so even an exponential storm is cheap. If
  frames are still alive after `len(links) + 2` hops, it's a storm.
- `reconcile()`: runs `stp_update()`, then brings addresses in line with the
  cabling, and returns notes describing what changed. The app calls it after every change.
- `auto_layout(width, mode=None, orientation=None, height=40)`: positions every
  device using the saved `layout_mode` and orientation unless supplied. The
  original layered/tree arrangement is retained for automatic orientation with
  no group overrides. Oriented layouts recursively arrange child groups first,
  then treat their complete frame rectangles as nodes. Layered/tree preserve
  kind/BFS levels on the induced graph; grid wraps; compact packs best-fit rows
  by decreasing block height; compact-layered shares the layered graph ordering
  with tighter block and level gaps; circular keeps its ring. Horizontal/vertical
  transposes algorithm coordinates and block dimensions together, keeping boxes
  upright. Vertical packing uses the canvas height as its wrap limit.
  `group_layouts` overrides direction inside individual groups.
  `LAYOUT_CHOICES` supplies the valid layouts
  and orientations; the command dispatches each argument to its setting by
  membership, regardless of order. Layouts use physical connections,
  independent of live state.
- `traversal(order, root)` / `renumber(order, root)`: BFS/DFS visit order,
  and renaming every device to match (returns `{old: new}`).

Code that holds on to a device across `changed()` should keep the
`Device` object, not its id: auto-renumbering may rename it (see `cmd_link`).

Device paths are lists of `(device_id, index_of_link_used_to_arrive)`, with
`-1` for the first device.

## The UI

`ByteSimApp` combines the four command mixins with Textual's `App`. The flow
for every command:

1. `on_input_submitted` → `run_command(line)`. Any `NetError` becomes a red
   console line.
2. `dispatch(tokens)` pulls out `key=value` options and applies the shortcuts
   (`r1` selects, `r1 s2` cables, a bare group name selects it). It then looks
   the word up in the table built by `registry.collect()`.
3. The handler changes the model, logs what happened, and calls
   `self.changed()`.
4. `changed()` runs `reconcile`, applies auto-renumbering if it's on, reports HA state changes, re-runs the layout
   if auto-arrange is on, and redraws the canvas and inspector.

`NetCanvas.render()` rebuilds the picture every frame, in layers: group
frames, then cables, then group labels, then device boxes, then address
labels, then packets. Each cable cell keeps a bitmask of the directions it
connects in (N/E/S/W), so crossings and junctions get the right box-drawing
character. A cell used only by control cables uses the double-line set; one
used only by fabric cables uses the heavy set.

Structural `remembering()` scopes cache port indexes, adjacency, bundle
membership and LAG ports, preserving cable order. They never cache liveness.
`route_query` adds a separate result cache for segments, addressed interfaces
and HA state during one synchronous `route()` call; it is discarded on success
or error before any model mutation or event-loop yield. `_plan` shares structural
indexes between routing and ARP flooding, leaving the scope before a storm
changes the model.

Packets are lists of screen cells, built from the canvas's cached cable geometry.
A 60 fps timer moves them at a common pace
and runs a callback on arrival, which is how ping chains its reply. The pace
is at least 45 cells per second at normal speed and scales with the longest
active path to finish it within 90 ticks (about 1.5 seconds at the target frame
rate). The session's `animation_speed` multiplier changes that shared pace;
fractional positions make slower playback smooth. `speed` accepts slow (0.5x),
normal (1x), fast (3x), or a numeric multiplier from 0.1 to 10.
Multicast copies share cells until their paths split, so a single packet
appears to fork.

Playback pause is session state on `CommandHost`. The shared interpreter
queues explicit packet-command tokens before calling their handlers, preserving
aliases and options. `resume` dispatches the batch without yielding to the
animation timer, so every new request starts at position zero on the next tick.
Small batches compute routes on dispatch. At 128 source/receiver pairs, multicast
uses generators that yield between route queries and packet creation. The canvas
drains preparation in 6 ms slices, scheduling each continuation with a positive
timer delay to yield the event loop. Playback waits for preparation to finish,
preserving the simultaneous start. The canvas freezes
packet positions, callbacks, flashes and its frame clock while paused;
`traffic_step` skips generation and load decay. The traffic timer still updates
its time reference, preventing a catch-up burst. Network replacement and
history restoration clear pending commands. Network/layout/mode edits cancel prepared
and in-flight work; a snapshot comparison avoids cancelling on unchanged refreshes.

Multicast ping uses the same request distribution as one-way multicast. On
each arrival, it asks the model for a fresh return route to that request's
source interface and starts a separate reply animation. Completion counts
replies and failures, and releases the request and reply cable highlights.
Completed packets are partitioned in linear time, with callbacks queued and
drained under the same work budget. `busy` includes preparation and callback
backlogs. Large batches default to per-source summaries; all simulation work still runs.
The shared handlers also run with the headless `ScriptRunner`.

`mping`'s trailing `*` modifier expands `all` selectors to addressed non-switch
devices (all nodes in graph mode), retaining configured failed devices for failure
accounting. Wildcards select devices once, while explicit address/port targets
normalize to `device:port` so interface choices survive multicast expansion.
Device-wide targets use the cheapest interface route, breaking ties by port
number; detailed request logs expose the chosen source and destination addresses.

## Scripts and synthesis

The command interpreter (`tui/commands/interpreter.py`) has no Textual
dependency. The app and `scripting.ScriptRunner` both use it.

- **`ScriptRunner`** is the real command mixins plus a stub canvas. It builds
  structure and skips layout, re-addressing and animation, so replaying a
  script is fast and uses exactly the app's command semantics.
- **`synthesis.synthesize(net, mode)`**:
  1. Renders an explicit plan: devices in id order, with placeholders for
     gaps, then groups, interfaces, cables, settings and failures.
  2. Lists reuse candidates. `clone` candidates are checked against the exact
     mapping `clone` would make. `copy` and `ports copy` candidates compare
     boundary patterns.
  3. Tries the best savings first. Each candidate is kept only if the
     re-rendered script replays into a network with no
     `equivalence.differences()` and is shorter.
- **The renderer** interleaves explicit `add`s and clones so every kind's ids
  are created in increasing order, and finishes a clone's source (groups,
  internal cables, settings) just before the clone.

## Adding a command

Add a method to the mixin that fits, or create a new mixin and add it to
`ByteSimApp`'s bases:

```python
from .registry import command

class TopologyCommands(CommandHost):
    @command("rename", "mv")
    def cmd_rename(self, args: list[str], opts: dict[str, str]) -> None:
        if len(args) != 2:
            raise NetError("usage: rename <id> <new-id>")
        ...                       # change self.net
        self.log_msg("renamed")
        self.changed()
```

Raise `NetError` for anything the user got wrong; it's shown in the console.
Put real behaviour in the model rather than the handler, so it can be tested
without the UI. Add a line to `tui/help.py` and to `docs/guide.md`.

## Tests

```sh
pytest
```

- `tests/test_topology.py`, `test_groups.py`, `test_addressing.py` and
  `test_ha.py`, `test_naming.py`, `test_stp.py` and `test_cloning.py` exercise
  the model directly, using the demo networks from
  `conftest.py`.
- `tests/test_app.py` drives the real app headlessly with Textual's
  `run_test()` and waits for packet animations to finish.

## Releasing

1. Bump `__version__` in `src/bytesim/__init__.py` and add a `CHANGELOG.md`
   entry.
2. `python -m build` creates `dist/bytesim-X.Y.Z.tar.gz` and the wheel.
3. `twine check dist/*`
4. `twine upload --repository testpypi dist/*` to try it, then
   `twine upload dist/*`.
