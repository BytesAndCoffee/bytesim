"""The text shown by the `help` command (Rich markup)."""

HELP = """\
[b]Commands[/]  (device ids: g1 r2 s3 c4 · ports: r1:3 or c2:nic2)
  [cyan]add[/] <g|r|s|c> \\[ports] \\[count]   add devices    e.g. [i]add s 24[/], [i]add c 2 3[/]
  [cyan]<id> <id>[/]                       cable two devices (first free ports)
  [cyan]<id>:<port> <id>:<port>[/]         cable specific ports, e.g. [i]r1:2 s1:8[/]
  [cyan]<id>[/]                            select device, then move it with arrow keys
  [cyan]unlink[/] <id>\\[:port] \\[id]         remove cable(s)
  [cyan]del[/] <id>                        delete a device
  [cyan]ports[/] <id…> <n>                 change port / NIC count, e.g. [i]ports s4 s6 8[/]
  [cyan]mesh[/] A B · [cyan]zip[/] A B · [cyan]parallel[/] a b n · [cyan]chain[/] x… · [cyan]ring[/] x…   cabling patterns
                                    A name is a device, a group or a,b,c, e.g. [i]mesh fab1 g1,g2,r2[/], [i]ring fab1[/]
  [cyan]ping[/] <id> <id|addr|internet|group|all> \\[dest…]   echo request + reply; several receivers = multicast
  [cyan]mping[/] <src|all> <ids|groups|all> \\[*]  shared requests + replies; [i]mping all all *[/] includes routers/gateways
                                    also [cyan]mcast ping[/]; large batches summarize per sender
                                    bare device targets choose fewest hops, then lowest port; [cyan]r1:p2[/] selects an interface
  [cyan]unicast[/] <src> <id|addr>         one-way packet to a single device
  [cyan]multicast[/] <src> <ids|groups|all> one packet, copied where paths split, e.g. [i]mcast c1 pcs c8[/]
  [cyan]group[/] <name> <id|group ...>      logical layer; groups nest, e.g. [i]group dc1 fab1 rfab1[/]
  [cyan]group[/] <name>  or  <name>         select a group (arrows / drag its frame move everything in it)
  [cyan]group[/] <name> layout=vertical|horizontal|auto   orient the selected layout inside this group
  [cyan]ungroup[/] <name> \\[member ...]      take members out, or dissolve (members move up a level)
  [cyan]clone[/] <group> \\[new-name]          copy a group: devices, nested groups, internal cables
  [cyan]ports copy[/] <group>(old, …) <group>(new, …)   cable an existing group the way another is cabled, e.g.
                                    [i]ports copy link1(rfab1, rfab2) link2(rfab2, rfab3)[/]
  [cyan]copy[/] <group>(old, …) <new>(new, …)   the same, creating the new group as a clone first
                                    a name like [i]fab(1)[/] means the next free fabN (group, clone)
  [cyan]renumber[/] bfs|dfs \\[root]           renumber each kind 1..n in BFS/DFS order from root (default g1…)
  [cyan]renumber auto[/] bfs|dfs \\[root] · [cyan]renumber off[/]   keep renumbering after every change
  [cyan]export[/] <file.yaml> \\[ports]          declarative topology (load reads it back); ports = exact
  [cyan]export script[/] \\[readable|shortest|explicit] \\[file]   the commands that rebuild this network
  [cyan]run[/] <file>                       run a script of commands (after [cyan]clear[/] for an export)
  [cyan]layout[/] \\[layered|tree|circular|grid|compact|compact-layered] \\[auto|horizontal|vertical]
                                    either or both, in any order; compact-layered keeps layers with tighter gaps
                                    orientation preserves the layout's structure; [cyan]layout list[/] shows choices
  [cyan]groups[/] · [cyan]auto[/] (re-arrange) · [cyan]list[/] · [cyan]demo[/] · [cyan]clear[/] · [cyan]save[/]/[cyan]load[/] \\[file] · [cyan]quit[/]
  [cyan]traffic on[/] \\[packets/s] \\[seed=N] \\[hosts=group] \\[mix=unicast,internet,broadcast]   random traffic
  [cyan]traffic[/] (statistics) · [cyan]traffic rate[/] <n> · [cyan]traffic reset[/] · [cyan]traffic off[/]
  [cyan]speed[/] \\[slow|normal|fast|0.1..10]   60 fps packet animation (normal = 1x; no argument shows it)
  [cyan]pause[/] · [cyan]resume[/] ([cyan]play[/])          freeze playback, queue packet actions, then launch them together
  [cyan]queue[/] · [cyan]queue clear[/]               inspect or discard queued actions
[b]Addressing[/]  an address is (network, host): network id + 8-bit host, e.g. [i](2,14)[/]
  MACs name the physical port: R1A = r1 port 1. A network spans every switch it touches.
  Routers/gateways take host 1, 2… on each network and serve DHCP; computers lease hosts 10+,
  or take a free host on their own when no server answers (ad-hoc)
  [cyan]ip[/] \\[id\\[:port]]                    table of MACs, networks and hosts
  [cyan]ip[/] <id:port> (net,host) \\[gw]      static, e.g. [i]ip c5 (2,50) 1[/]  ·  [cyan]ip[/] <id:port> dhcp|adhoc|none
  [cyan]dhcp[/] <id\\[:nic]|all>                run DHCP (animated DISCOVER broadcast + OFFER)
  [cyan]release[/] <id\\[:nic]|all>             give up a lease  ·  [cyan]adhoc[/] <id\\[:nic]|all>  self-assign
  [cyan]dhcpd[/] <router> on|off             turn a router's DHCP server on or off
  [cyan]mode[/] graph|addr                   graph mode: pure connectivity, no addressing rules
[b]Loops, STP, storms[/]  switches run spanning tree unless it's switched off for them
  [cyan]broadcast[/] <id\\[:port]>                 flood one frame; into a live loop it becomes a storm
  [cyan]lag[/] (find spans) · [cyan]lag auto[/] · [cyan]lag[/] <switch|domain> <switch|router|gateway|domain> · [cyan]unlag[/] … · [cyan]lags[/]
  [cyan]mlag[/] <group…> · [cyan]unmlag[/] <group> · [cyan]mlags[/]   a group of switches acts as one (peer links needed), like vPC
                                    bundle parallel cables into one link (no STP blocking, flows spread)
  [cyan]stp[/]  ·  [cyan]stp[/] on|off  ·  [cyan]stp[/] <switch…> on|off  ·  [cyan]stp priority[/] <switch> <n>  ·  [cyan]demo loop[/]
[b]Roles, failures, HA[/]
  [cyan]<id> <id> role=[/]data|control|control-backup|fabric   cable with a role (control = heartbeat only, ═)
  [cyan]role[/] <id:port>|<id> <id> <role>       change a cable's role  (fabric = router↔router data, ━)
  [cyan]fail[/] <id> | <id:port> | <id> <id>     kill a device or cable(s) · [cyan]restore[/] … | all
  [cyan]cluster[/] <name> <routers…> \\[policy=none|quorum|witness] \\[mode=active-active|active-passive]
                                    HA pair/group sharing gateway host 254; active-passive: only the active forwards
  [cyan]cluster[/] <name>  ·  [cyan]clusters[/]  ·  [cyan]uncluster[/] <name>  ·  [cyan]demo ha[/]
  [cyan]undo[/] \\[n] · [cyan]redo[/] \\[n] · [cyan]history[/]   step back through changes (u / U on the canvas)
[b]Canvas keys[/]  arrows move (shift = x4) · tab cycle · a arrange · x delete · u undo · U redo
  hjkl / drag background / wheel pan · drag a box to move · v show/hide addresses · enter or : → command line"""
