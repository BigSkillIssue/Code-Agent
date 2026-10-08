"""The `forge-sandbox` command: the daemon and its helpers inside a project's container."""

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from forge_sandbox import __version__
from forge_sandbox.fsops import Owner

DEFAULT_SOCKET = Path("/run/forge-sandbox/daemon.sock")


def build_parser() -> argparse.ArgumentParser:
    """The argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="forge-sandbox", description="Forge Web sandbox runtime (runs inside a container)."
    )
    parser.add_argument("--version", action="version", version=f"forge-sandbox {__version__}")
    commands = parser.add_subparsers(dest="command")
    daemon = commands.add_parser("daemon", help="serve a workspace to the server")
    daemon.add_argument("--workspace", type=Path, required=True, help="the project folder")
    how = daemon.add_mutually_exclusive_group(required=True)
    how.add_argument("--stdio", action="store_true", help="one connection on stdin/stdout")
    how.add_argument("--socket", type=Path, help="listen on this unix socket")
    daemon.add_argument("--owner", help="UID:GID that owns the workspace (when run as root)")
    attach = commands.add_parser("attach", help="relay stdin/stdout to the daemon's socket")
    attach.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    return parser


def parse_owner(text: str | None) -> Owner | None:
    """'1000:1000' -> Owner(1000, 1000)."""
    if not text:
        return None
    uid, _, gid = text.partition(":")
    return Owner(int(uid), int(gid or uid))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # stdout may be the protocol stream: everything people read goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "daemon":
        return run_daemon(args)
    if args.command == "attach":
        from forge_sandbox.attach import relay

        return asyncio.run(relay(args.socket))
    parser.print_help(sys.stderr)
    return 0


def run_daemon(args: argparse.Namespace) -> int:
    """`forge-sandbox daemon`."""
    from forge_sandbox.daemon import Daemon, serve_socket, serve_stdio

    workspace: Path = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    daemon = Daemon(workspace, owner=parse_owner(args.owner))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_stdio(daemon) if args.stdio else serve_socket(daemon, args.socket))
    return 0
