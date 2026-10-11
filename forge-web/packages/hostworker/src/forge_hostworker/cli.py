"""`forge-host-worker`: run a Linux server's apps for a Forge Web server.

    forge-host-worker keygen --data DIR       make the host's key (once); prints the public key
    forge-host-worker doctor --data DIR       is this host ready? names every missing piece
    forge-host-worker firewall [--apply] [--allow ADDR ...]   print or (root) load the rules
    forge-host-worker run --server URL --data DIR [--token-file FILE] [--slots N] [--docker PATH]
                          [--apps-domain DOMAIN --legal-base URL]   (with Caddy as the edge)

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

from forge_hostworker import __version__, firewall
from forge_hostworker.client import HostClient
from forge_hostworker.docker import DockerCli, HostError, container_owner, require_runsc
from forge_hostworker.doctor import TEST_IMAGE, doctor, is_root, report
from forge_hostworker.edge import Edge
from forge_hostworker.runner import HostRunner, default_user
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
    check = commands.add_parser("doctor", help="check that this host can run apps")
    check.add_argument("--data", type=Path, required=True)
    check.add_argument("--docker", default="docker")
    check.add_argument("--test-image", default=TEST_IMAGE)
    wall = commands.add_parser("firewall", help="the rules for app containers")
    wall.add_argument("--apply", action="store_true", help="load them (as root)")
    wall.add_argument("--allow", action="append", default=[], help="an address apps may reach")
    run = commands.add_parser("run", help="take jobs from the server")
    run.add_argument("--server", required=True, help="Forge Web's URL, https://...")
    run.add_argument("--data", type=Path, required=True, help="where releases and keys live")
    run.add_argument("--token-file", type=Path)
    run.add_argument("--slots", type=int, default=2)
    run.add_argument("--docker", default="docker")
    run.add_argument("--apps-domain", help="apps answer at <app>.DOMAIN (Caddy is the edge)")
    run.add_argument("--legal-base", help="Forge Web's legal pages, https://forge.../legal")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "keygen":
        return make_key(args.data)
    if args.command == "firewall":
        return set_firewall(tuple(args.allow), args.apply)
    if args.command == "doctor":
        checks = asyncio.run(doctor(DockerCli(args.docker), args.data, image=args.test_image))
        print(report(checks))
        return 0 if all(c.ok for c in checks) else 1
    if (args.apps_domain is None) != (args.legal_base is None):
        parser.error("--apps-domain and --legal-base go together")
    return asyncio.run(serve(args))


def set_firewall(allow: tuple[str, ...], load: bool) -> int:
    """Print the rules, or load them as root."""
    if not load:
        print(firewall.restore_script(allow), end="")
        return 0
    try:
        for command in firewall.apply(allow):
            print(f"ran: {command}")
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


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
    user = default_user()
    try:
        await require_runsc(docker)
        owner = await container_owner(docker, user, is_root())
    except HostError as error:
        print(f"error: {error}\nhint: {error.hint}", file=sys.stderr)
        return 2
    runner = HostRunner(docker, args.data, user=user, owner=owner)
    client = HostClient(args.server, token, runner, load_key(args.data / KEY), slots=args.slots)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.apps_domain is None:
        await client.serve(stop)
        return 0
    edge = Edge(runner, docker, args.apps_domain, args.legal_base)
    runner.on_change = edge.changed
    ask = await edge.serve_ask()
    try:
        await asyncio.gather(client.serve(stop), edge.keep_fresh(stop))
    finally:
        ask.close()
        await edge.close()
    return 0
