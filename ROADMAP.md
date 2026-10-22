# Roadmap

Planned work that isn't scheduled into a release yet. Shipped work goes in the
[changelog](CHANGELOG.md).

The items below are user-interface work on top of the command language: they
don't change packet behaviour, the commands or the file formats.

## TODO

- [ ] **Point-and-click editing that produces commands.** Clicks build an
  ordinary console command and run it through the same interpreter as typed
  ones, so undo, `history`, `tee` logs, `export script` and replays all keep
  working, and the console shows the command (`› r1:3 s2:5`) so people learn
  the syntax by watching.
  - Wiring: click a port, then another, for `r1:3 s2:5`; click a device, then
    another, for `r1 s2` (first free ports).
  - Adding: pick a kind from a palette (`g` `r` `s` `c` keys, or a footer
    strip), then click empty space for `add r`, placed where you clicked.
  - Safe actions (add, cable, select) run at once. Destructive ones (delete,
    unlink, fail) only fill in the command line (`del s2`) and wait for Enter.
  - Selecting and dragging stay as they are: they're layout, not network
    changes.
- [ ] **Pop-out panels (experimental).** `pop console` / `pop inspector` open a
  panel in a separate terminal window: the app serves a local socket, and
  `bytesim attach <panel>` is a thin viewer for it (with a command line, for
  the console). Falls back to printing the `attach` command where no terminal
  can be launched (tmux, SSH). Versioned protocol, no compatibility promise
  while experimental; a view-only canvas mirror would come later, if wanted.
