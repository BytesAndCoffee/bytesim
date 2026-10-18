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
│   ├── layout.py        automatic arrangement
│   ├── naming.py        BFS/DFS traversal, renumber()
│   ├── stp.py           spanning tree, loop detection, flooding, storms
│   ├── equivalence.py   canonical form of a network; differences()
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
        └── traffic.py   ping, unicast, multicast
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
- `auto_layout(width)`: positions every device.
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

Packets are lists of screen cells. A timer moves every packet forward one
cell per tick and runs a callback when it arrives, which is how ping chains
its reply. Multicast copies share cells until their paths split, so a single
packet appears to fork.

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
