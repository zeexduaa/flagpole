"""Command-line interface (filled in as features land)."""

from __future__ import annotations

import argparse

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flagpole")
    parser.add_argument("--version", action="version", version=f"flagpole {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
