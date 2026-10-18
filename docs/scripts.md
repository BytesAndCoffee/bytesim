# Construction scripts

ByteSim can tell you **how to build the network you're looking at**, written in
the same commands you type at the console:

```text
export script                      show it in the console (readable)
export script shortest site.txt    write the most compact script found
export script explicit site.txt    one step at a time, no reuse
run site.txt                       replay a script (after `clear` for an export)
```

A script isn't a list of everything in the network. Where parts of the network
repeat, it builds one part and **reuses** it with `clone`, `copy` or
`ports copy`. Reading the script shows the network's patterns: "fab3 is fab1
again, cabled the same way to gate2 and rfab2".

## Three ways to save a network

| | `save` (JSON) | `export` (YAML topology) | `export script` (commands) |
|---|---|---|---|
| Meant for | resuming a session | describing an architecture | explaining how to build it |
| Reads back with | `load` | `load` | `run` |
| Keeps positions, addresses | yes | no | no |
| Keeps exact device ids and ports | yes | ids yes, ports with `ports` | yes |
| Written in | JSON | templates and link macros | ordinary console commands |

## Tutorial: build → export → reset → replay → compare

```text
demo ha                            1. build something (or load your own network)
fail r1:5                             (failures are rebuilt too)
export script readable ha.txt      2. export it
clear                              3. reset
run ha.txt                         4. replay
ping c1 c6                         5. it behaves exactly the same
```

Open `ha.txt` in an editor to read it. It starts like this:

```text
# Devices: add <kind> <ports> <count> creates the next ids of that kind
add r 6 2
add s 6 2
add c 1 6

# Finish fab1 and the groups its copy connects to so it can be reused
group fab1 s1 s2
group rfab1 r1 r2
cluster rfab1 r1 policy=none
s1:1 s2:1
r1:1 s1:2
r2:1 s2:2

# fab2 is fab1 again, cabled the same way with rfab1→rfab1: copy it
copy fab1(rfab1) fab2(rfab1)
...
```

You don't have to compare by hand. ByteSim compares every script before giving
it to you (see [Verified, always](#verified-always)).

For a bigger example, `load demo4_topology.yaml` (three sites and a transit
fabric). `export script shortest` rebuilds its 44 devices and 86 cables in 49
commands instead of 122, using `copy` for repeated fabrics and offices and
cabling patterns like `mesh` for the rest.

## What a script rebuilds

- every device with its exact id, kind and port count. Deleted ids are
  recreated as placeholders and deleted again, so the numbering, and the id the
  next new device gets, match;
- every group, its nesting and member order;
- every cable on its exact ports, with its role (data, control, fabric);
- HA clusters and their policies, STP settings (off, priority), DHCP servers
  switched off, interface settings (static addresses, ad-hoc, off);
- failures (in a last section, *Runtime state*).

It doesn't rebuild:

- positions (the layout is recomputed);
- addresses learned by DHCP or ad-hoc (re-learned when the script finishes);
- MAC tables;
- storms.

If something can't be expressed as a command at all, the script says so in a
`# NOT EXPRESSIBLE:` line, and `export script` repeats it in the console. One
example is an HA cluster with no router directly in its group, because
`cluster` needs one to name.

## The three modes

- **`readable`** (default): sections with comments, and reuse that saves at
  least three commands, always building later groups from earlier ones.
- **`shortest`**: any reuse that makes the script shorter, without comments.
  "Shortest" means the shortest script ByteSim found within its search limits,
  not a proven minimum.
- **`explicit`**: no reuse at all, so every device, group and cable is spelled
  out. Useful for learning the basic commands, and it's the fallback the other
  modes start from.

## How reuse is found

- **`clone A B`** makes a copy of group A called B. It always numbers the copies
  the same way: A's devices, in id order, get the next free ids. So B can be
  built by cloning A only if:
  - B's ids are exactly the ones those next ids would be;
  - that pairing keeps every port count, internal cable (ports and roles
    included), nested group, cluster and setting;
  - the nested groups end up with B's names.
- **`copy A(X, Y) B(P, Q)`** is a clone that also cables B to P and Q the way A
  is cabled to X and Y.
- **`ports copy A(X, Y) B(P, Q)`** cables an existing B to P and Q the way A is
  cabled to X and Y. Devices are paired by position in each group.
- **Cabling patterns** replace runs of cables: `mesh A B` (every device of A to
  every device of B, where A or B is a group or a list like `g1,g2,r2`),
  `ring fab1` and `parallel s4 s6 2`. A pattern takes each device's next free
  port, so it's placed where those ports come out right. That's either where its
  first cable would be, or after the `ports copy` lines, ordered so lower ports
  are made first.
- **Devices** are added in runs: `add s 7 23` followed by
  `ports s4 s6 s13 8` for the odd ones out, when that's shorter than one `add`
  per port count.
- **MLAG domains** are declared in one line (`mlag fab1 fab2 …`), and when the
  bundles are exactly the spans `lag auto` would make, that one command replaces
  a `lag` line per bundle.

A quick structural check finds candidates. Each candidate is then tried for
real, by writing the script with it and replaying it. It's kept only if the
replay matches and the script got shorter.

## Verified, always

Every script is replayed by ByteSim's own command handlers into a fresh,
empty network and compared with the original before you see it. The
comparison covers device ids, kinds and port counts; id counters; groups and
member order; cables with their ports and roles; clusters; STP, DHCP and
interface settings; and failures. A candidate that would rebuild anything
differently is thrown away. If even the explicit script couldn't rebuild the
network, `export script` reports an error instead of writing a wrong script.

## Running scripts

`run <file>` runs one command per line. Blank lines and anything after `#` are
ignored. While a script runs, the screen and console wait until it's finished,
and DHCP happens once at the end.

If a line fails, `run` stops and names the line, for example
`line 3 (frobnicate): unknown command`. Everything before that line has
already happened.

Scripts aren't only for exports. You can write your own lab setups, or keep a
list of commands to repeat.
