# ByteSim

[![CI](https://github.com/BytesAndCoffee/bytesim/actions/workflows/ci.yml/badge.svg)](https://github.com/BytesAndCoffee/bytesim/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bytesim)](https://pypi.org/project/bytesim/)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)](https://pypi.org/project/bytesim/)

**A network simulator that runs in your terminal.** Add gateways, routers,
switches and computers, cable them together by typing device ids, and watch
DHCP, routing, multicast and router failover happen packet by packet.

```
                     ╭┄ rfab1 · HA ┄┄┄┄┄┄┄┄┄┄┄┄╮
                     ┆ ┌────────┐    ┌────────┐ ┆
                     ┆ │   r1   │    │   r2   │ ┆
                     ┆ │ ●●○●●● │    │ ●●○●●● │ ┆
                     ┆ └─┬┬─┬┬┬─┘    └─┬┬─┬┬┬─┘ ┆
                     ╰┄┄┄││┄┃║║┄┄┄┄┄┄┄┄││┄┃║║┄┄┄╯
          ┌──────────────┘└─┴┴┼──────────┼┼─┴┤║
          │             ┌─────┴──────────┴┴──┼┴────────────┐
     ╭┄┄┄┄│ fab1 ┄┄┄┄┄┄┄│┄┄┄┄┄┄┄╮      ╭┄┄┄┄│ fab2 ┄┄┄┄┄┄┄│┄┄┄┄┄┄┄╮
     ┆ ┌──┴─────┐    ┌──┴─────┐ ┆      ┆ ┌──┴─────┐    ┌──┴─────┐ ┆
     ┆ │   s1   │    │   s2   │ ┆      ┆ │   s3   │    │   s4   │ ┆
     ┆ │ ●●●●○○ │    │ ●●●○○○ │ ┆      ┆ │ ●●●●○○ │    │ ●●●○○○ │ ┆
     ┆ └─┬─┬┬───┘    └─┬─┬────┘ ┆      ┆ └─┬─┬┬───┘    └─┬─┬────┘ ┆
     ╰┄┄┄│┄││┄┄┄┄┄┄┄┄┄┄│┄│┄┄┄┄┄┄╯      ╰┄┄┄│┄││┄┄┄┄┄┄┄┄┄┄│┄│┄┄┄┄┄┄╯
         └─●•••─────┬─┘ │                  │ ││          │ └─────┐
         ┌─┴──┐    ┌┴───┐    ┌────┐      ┌─┴──┐    ┌────┐    ┌────┐
         │ c1 │    │ c2 │    │ c3 │      │ c4 │    │ c5 │    │ c6 │
         └────┘    └────┘    └────┘      └────┘    └────┘    └────┘
         (1,10)    (1,11)    (1,12)      (2,10)    (2,11)    (2,12)
```

ByteSim keeps what matters about networking (graphs, broadcast domains,
forwarding, failure) and leaves out the detail that gets in the way:

- **Devices with real ports.** Routers and switches can have any number of
  ports, and computers can have several network cards. Every port shows on
  the device as `○` (free) or `●` (in use).
- **Cabling by typing.** `r1 s2` connects two devices using their first free
  ports. `r1:3 s2:8` picks the ports.
- **Automatic layout**, with arrow keys or the mouse to move things yourself.
- **Simple addressing.** An address is a pair: a network id and an 8-bit host
  number, like `(2,14)`. MAC addresses name the physical port: `R1A` is port 1
  of router r1.
- **DHCP and ad-hoc addressing**, animated as broadcast-then-reply.
- **Routing** through gateways and between routers, with ARP shown for every hop.
- **Unicast, ping and multicast.** With multicast you can see the packet copied
  wherever the paths split.
- **Cable roles** (data, control, fabric) and failures you can trigger by hand.
- **Switching loops**: spanning tree with root bridges and blocked ports, and
  real broadcast storms (frames multiplying, MAC tables flapping) when STP is off.
- **HA router clusters** with a shared virtual gateway, failover, split-brain,
  and quorum or witness policies.
- **Nested groups** drawn as labelled frames, which you can **clone** along with
  their internal cables.
- **BFS/DFS renumbering**, once or automatically after every change, so ids
  follow the network's shape.
- **Graph mode**, which ignores addressing so you can explore pure connectivity.
- **Topology files.** Describe networks in YAML with reusable templates and link
  macros (`mesh: [routers, switches]`), then load or export them.
- **Construction scripts.** `export script` writes the console commands that
  rebuild your network, reusing repeated parts with `clone` and `copy`. Each
  script is checked by replaying it before you get it, and `run` replays it.

## Install

```sh
pip install bytesim
```

Requires Python 3.10+ and a terminal with Unicode and colour support.

## Quick start

```sh
bytesim --demo basic        # a small routed network
bytesim --demo ha           # a clustered router pair between two fabrics
bytesim --demo loop         # a switch loop with STP off: set off a storm
bytesim mynet.json          # open a saved network
bytesim campus.yaml         # build one from a topology file
```

Then, at the command line at the bottom of the screen:

```text
add r 4            add a router with 4 ports        → r1
add s 8            add an 8-port switch             → s1
add c 1 3          add three computers with 1 NIC   → c1 c2 c3
r1 s1              cable r1 to s1
s1 c1              cable s1 to c1 (c1 gets an address by DHCP)
ping c1 c2         watch the echo request and reply
```

Type `help` for every command. Press `Esc` to switch from the command line to
the canvas, where the arrow keys move the selected device and `Tab` cycles
through devices.

## Documentation

- [User guide](docs/guide.md): concepts, every command, keys, worked examples
- [Architecture](docs/architecture.md): how the code is organised, and how to add a command
- [Topology format](docs/topology-format.md): templates and link macros in YAML
- [Construction scripts](docs/scripts.md): `export script` and `run`, with a tutorial
- [Save file format](docs/file-format.md)
- [Changelog](CHANGELOG.md)

## Using the model from Python

The simulation is a plain Python model with no UI dependency:

```python
from bytesim import Network

net = Network()
net.add("r"); net.add("s"); net.add("c"); net.add("c")
net.connect("r1", None, "s1", None)
net.connect("s1", None, "c1", None)
net.connect("s1", None, "c2", None)
net.reconcile()                       # router autoconfig + DHCP

print(net.iface("c1", 1).addr)        # (1, 10)
for src, dst, frames in net.route("c1", ("c2", 1)):
    print(src, "→", dst, [d for d, _ in frames])
```

## Development

```sh
git clone https://github.com/BytesAndCoffee/bytesim
cd bytesim
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m build && twine check dist/*
```

## License

MIT © Michael Yazdani. See [LICENSE](LICENSE).
