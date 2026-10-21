# ByteSim: start from a blank canvas

Build two small networks with four computers, two switches and one router.
Then see which traffic needs the router and which can stay on its own network.

## Open an empty network

With ByteSim installed, run this in your terminal:

```sh
bytesim
```

The canvas starts empty. From here on, enter commands at the command line
inside ByteSim, one at a time.

## Add the devices

```text
add c 1 4
add s 8 2
add r
```

You now have:

- `c1`–`c4`: four computers, each with one network card.
- `s1` and `s2`: two switches, each with eight ports.
- `r1`: one router with its default four ports.

The numbers after a device kind are its port count and how many to add.
For example, `add c 1 4` means four computers with one port each.

## Cable them together

Build this arrangement:

```
c1 --- s1 --- r1 --- s2 --- c3
       |            |
       c2           c4
```

First, connect the router to both switches:

```text
r1 s1
r1 s2
```

Then connect two computers to each switch:

```text
s1 c1
s1 c2
s2 c3
s2 c4
```

Typing two device names connects their first free ports. The router supplies
addresses through DHCP as you connect the computers. Wait for each exchange
to finish before continuing.

Click a device to inspect it. Press `Esc` to focus the canvas: `Tab` selects
the next device and the arrow keys move it. `Enter` returns to the command
line.
The `auto` command arranges everything again.

## Look at the addresses

```text
ip
```

An address is written `(network,host)`. The two computers on s1 share a
network number; the two on s2 share a different one. Each computer also has
a gateway address pointing to the router on its own network.

You do not need to enter addresses or routes for this lab.

## Send some packets

First, send between computers on the same switch:

```text
ping c1 c2
ping c3 c4
```

Watch each request and reply, and wait for its result before the next command.
These packets stay on their local networks.

Now send between the two networks:

```text
ping c1 c3
traceroute c1 c3
```

This traffic crosses r1. The router connects the two networks, and
`traceroute` shows it on the way to c3.

## Find out what the router does

Turn r1 off:

```text
fail r1
```

Repeat the local pings:

```text
ping c1 c2
ping c3 c4
```

Both still work. The computers already have addresses, and their switches
can deliver traffic within each network without the router.

Try crossing between networks:

```text
ping c1 c3
```

This fails: there is no working router between the two networks. Restore r1
and try again:

```text
restore r1
ping c1 c3
```

The reply returns again. The details mention that s1 flooded the frame for
`R1A`: the switch forgot r1's address when that link went down, so it floods
the first frame until it learns where r1 is again.

## Save your network

```text
save clean-lab.json
```

The file is saved in ByteSim's working directory. Press `Ctrl+Q` to quit.
To reopen it from that directory in your terminal:

```sh
bytesim clean-lab.json
```

Use `help` inside ByteSim for the command summary, or the
[user guide](guide.md) for explanations and the command families.
