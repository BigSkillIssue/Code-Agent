"""The `forge-sandbox` command: the daemon and its helpers inside a project's container."""

import argparse
from collections.abc import Sequence

from forge_sandbox import __version__


def build_parser() -> argparse.ArgumentParser:
    """The argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="forge-sandbox", description="Forge Web sandbox runtime (runs inside a container)."
    )
    parser.add_argument("--version", action="version", version=f"forge-sandbox {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
