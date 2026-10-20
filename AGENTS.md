# ByteSim contributor guidance

ByteSim is a terminal network simulator written in Python 3.10+ with a Textual
UI. Read the relevant documentation before changing behavior:

- [README.md](README.md): overview, installation, and quick start.
- [docs/guide.md](docs/guide.md): simulation concepts, commands, and controls.
- [docs/architecture.md](docs/architecture.md): code structure and extension points.
- [docs/topology-format.md](docs/topology-format.md): declarative YAML topologies.
- [docs/scripts.md](docs/scripts.md): construction scripts and verified export.
- [docs/file-format.md](docs/file-format.md): JSON snapshots and compatibility.
- [CHANGELOG.md](CHANGELOG.md): release history.

## Code structure

- `src/bytesim/model/` contains the simulation. `Network` combines concern-specific
  mixins sharing `NetworkBase` state. Keep the model free of UI dependencies;
  put simulation behavior here so Python callers and tests can use it directly.
- `src/bytesim/tui/` contains the Textual app, canvas, inspector, and commands.
  Commands use the `@command` registry and raise `NetError` for user errors.
- `src/bytesim/tui/commands/interpreter.py` provides the shared interpreter.
  Both `ByteSimApp` and the headless `ScriptRunner` use the command handlers.
  Wire new command mixins into each host that needs them.
- `src/bytesim/topology.py` loads and exports declarative topologies.
  Console and YAML cabling patterns share `model/macros.py`.
- `src/bytesim/scripting.py` replays commands headlessly.
  `src/bytesim/synthesis.py` exports construction scripts and verifies them by
  replay, using `model/equivalence.py` to compare the rebuilt network.
- `tests/` covers model behavior, topology and script round trips, and the real
  app through Textual's headless `run_test()`.

## Behavior to preserve

- Addresses are `(network id, 8-bit host)` pairs. Ports start at 1; device ids
  count independently by kind. Preserve counters and exact port assignments
  where the format promises them.
- Forwarding must respect failures, cable roles, STP, storms, aggregation,
  MLAG, and HA state. Use the model's shared connectivity helpers.
- After UI edits, use `changed()` to reconcile addressing, renumber, report HA
  changes, arrange, and redraw. Script execution batches these side effects.
- Auto-renumbering can change device ids during `changed()`. Keep a `Device`
  object when tracking a device across that call.
- JSON saves are session snapshots; YAML topologies describe architecture;
  construction scripts rebuild the structure and settings promised by their
  documentation. Keep these contracts and older-save compatibility intact.
- Keep script export verification: replay candidates and reject changes that
  rebuild a different network. Report settings the command language cannot
  express. The `shortest` mode is a bounded search, not a guaranteed minimum.

## Making changes

- Follow the existing mixin structure and typing style. Runtime dependencies
  are declared in `pyproject.toml`; the current ones are Textual and PyYAML.
- For commands or user-visible behavior, update `src/bytesim/tui/help.py` and
  the relevant documentation. Record release-worthy changes in `CHANGELOG.md`.
- Add or adjust tests for changed behavior. Use model tests for simulation
  rules, round-trip tests for formats and synthesis, and headless app tests
  for UI integration.
- Preserve unrelated local changes and untracked files.

## Development and validation

Install the development dependencies in a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the checks relevant to the change:

```sh
python -m pyflakes src tests
python -m pytest -q
```

For packaging changes or release preparation:

```sh
python -m build
python -m twine check dist/*
```

CI runs lint and tests on Linux, macOS, and Windows with Python 3.10–3.14,
plus a package build check. The version lives in `src/bytesim/__init__.py`.
