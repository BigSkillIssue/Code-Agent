"""`forge-host-worker`: run a Linux server's apps for a Forge Web server.

    forge-host-worker keygen --data DIR       make the host's key (once); prints the public key
    forge-host-worker run --server URL --data DIR [--token-file FILE] [--slots N] [--docker PATH]

The token comes from --token-file (default DIR/token) or FORGE_HOST_TOKEN; the admin makes it
in Forge Web and registers the public key there.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from forge_hostworker import __version__
from forge_hostworker.client import HostClient
from forge_hostworker.docker import DockerCli, HostError, require_runsc
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import TOKEN_PREFIX, public_key

KEY = "host.key"


def main(argv: list[str] | None = None) -> int:
    """Run the command line; the exit code."""
    parser = argparse.ArgumentParser(
        prog="forge-host-worker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"forge-host-worker {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="make the host's key")
    keygen.add_argument("--data", type=Path, required=True)
    run = commands.add_parser("run", help="take jobs from the server")
    run.add_argument("--server", required=True, help="Forge Web's URL, https://...")
    run.add_argument("--data", type=Path, required=True, help="where releases and keys live")
    run.add_argument("--token-file", type=Path)
    run.add_argument("--slots", type=int, default=2)
    run.add_argument("--docker", default="docker")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "keygen":
        return make_key(args.data)
    return asyncio.run(serve(args))


def make_key(data: Path) -> int:
    """Make the host's X25519 key once; print its public half."""
    path = data / KEY
    if path.exists():
        print(f"{path} exists; its public key: {public_key(load_key(path))}")
        return 0
    data.mkdir(parents=True, exist_ok=True)
    private = X25519PrivateKey.generate()
    path.touch(mode=0o600)
    path.write_bytes(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    print(f"made {path}; register this public key in Forge Web: {public_key(private)}")
    return 0


def load_key(path: Path) -> X25519PrivateKey:
    """The host's private key."""
    return X25519PrivateKey.from_private_bytes(path.read_bytes())


def read_token(args: argparse.Namespace) -> str:
    """The host token from its file or the environment; '' when there is none."""
    path: Path = args.token_file or args.data / "token"
    token = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    return token or os.environ.get("FORGE_HOST_TOKEN", "").strip()


async def serve(args: argparse.Namespace) -> int:
    """Check the host, then take jobs until a signal stops the worker."""
    token = read_token(args)
    if not token.startswith(TOKEN_PREFIX):
        print(
            f"error: no host token (it starts with {TOKEN_PREFIX}); make one in Forge Web's "
            "admin pages and put it into --token-file or FORGE_HOST_TOKEN",
            file=sys.stderr,
        )
        return 2
    if not (args.data / KEY).is_file():
        print(
            "error: no host key; run `forge-host-worker keygen --data ...` first", file=sys.stderr
        )
        return 2
    docker = DockerCli(args.docker)
    try:
        await require_runsc(docker)
    except HostError as error:
        print(f"error: {error}\nhint: {error.hint}", file=sys.stderr)
        return 2
    runner = HostRunner(docker, args.data)
    client = HostClient(args.server, token, runner, load_key(args.data / KEY), slots=args.slots)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await client.serve(stop)
    return 0
