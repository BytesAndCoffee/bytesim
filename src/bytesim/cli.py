"""Command-line entry point: ``bytesim [FILE] [--demo NAME] [--run CMD ...]``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bytesim",
        description="A text-mode network simulator: gateways, routers, switches and computers, "
                    "cabled together and simulated in your terminal.")
    p.add_argument("file", nargs="?", type=Path,
                   help="a saved network (.json) or a topology (.yaml) to open")
    p.add_argument("--demo", choices=["basic", "ha", "loop"], help="start with a sample network")
    p.add_argument("--run", "-c", metavar="CMD", action="append", default=[],
                   help="a console command to run at startup (repeatable)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    from .model import NetError, Network
    from .topology import load_topology, looks_like_topology
    from .tui import ByteSimApp

    network, topology = None, False
    if args.file is not None:
        try:
            text = args.file.read_text()
            topology = looks_like_topology(text)
            network = load_topology(text) if topology else Network.from_json(text)
        except (OSError, ValueError, KeyError, TypeError, NetError) as e:
            sys.exit(f"bytesim: couldn't open {args.file}: {e}")
    startup = ([f"demo {args.demo}"] if args.demo else []) + args.run
    # A topology carries no positions, so it starts auto-arranged.
    ByteSimApp(network=network, startup=startup, auto=True if topology else None).run()


if __name__ == "__main__":
    main()
