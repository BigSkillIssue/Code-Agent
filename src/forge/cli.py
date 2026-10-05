"""Command-line entry point for Forge."""

import argparse
from collections.abc import Sequence

from forge import __version__


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="forge", description="Forge coding agent.")
    parser.add_argument("--version", action="version", version=f"forge {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
