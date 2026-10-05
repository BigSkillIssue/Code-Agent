"""Command-line entry point for Forge."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from forge import __version__
from forge.config import ConfigError, find_project_root, load_config, render_effective


def build_parser() -> argparse.ArgumentParser:
    """Create the parser for options that apply to every command."""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge coding agent. Run `forge <command> --help` for a command's options.",
        epilog="Commands: config check",
    )
    parser.add_argument("--version", action="version", version=f"forge {__version__}")
    parser.add_argument("-p", "--profile", help="config profile to apply, e.g. 'ci'")
    parser.add_argument("-C", "--cwd", type=Path, help="run as if started in this folder")
    return parser


def cmd_config(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge config check`: validate and print the effective configuration."""
    parser = argparse.ArgumentParser(prog="forge config")
    parser.add_argument("action", choices=["check"])
    parser.parse_args(rest)
    root = find_project_root(options.cwd or Path.cwd())
    try:
        cfg = load_config(root, profile=options.profile)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(render_effective(cfg), end="")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, list[str]], int]] = {
    "config": cmd_config,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    options, rest = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if rest and rest[0] in COMMANDS:
        return COMMANDS[rest[0]](options, rest[1:])
    parser.print_help()
    return 0
