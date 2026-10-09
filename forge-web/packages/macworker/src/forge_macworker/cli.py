"""`forge-mac-worker`: build Apple apps for a Forge Web server on this Mac.

  forge-mac-worker check [--image NAME] [--direct]
  forge-mac-worker prepare-image --from IMAGE --name NAME --wheels FOLDER
  forge-mac-worker run --server URL [--token-file FILE] [--image NAME | --direct] [--slots N]

The token comes from the admin page (Admin > Apple > Add a Mac): put it into a file only you can
read and pass `--token-file`, or set MAC_WORKER_TOKEN.
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from forge_macworker import __version__
from forge_macworker.client import WorkerClient
from forge_macworker.images import check_setup, prepare_image
from forge_macworker.runners import DirectRunner, Runner, TartRunner, TartSettings

TOKEN_ENV = "MAC_WORKER_TOKEN"  # not FORGE_*: Forge reads those as its own settings


def parser() -> argparse.ArgumentParser:
    """The command line."""
    top = argparse.ArgumentParser(prog="forge-mac-worker", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    top.add_argument("--version", action="version", version=f"forge-mac-worker {__version__}")
    commands = top.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="is this Mac ready to build?")
    check.add_argument("--image", default="forge-xcode", help="the prepared Tart image")
    check.add_argument("--direct", action="store_true", help="check for direct mode (no VMs)")
    prepare = commands.add_parser("prepare-image", help="make the VM image jobs run in")
    example = "ghcr.io/cirruslabs/macos-sequoia-xcode:latest"
    prepare.add_argument("--from", dest="base", required=True,
                         help=f"a macOS image with Xcode, e.g. {example}")  # fmt: skip
    prepare.add_argument("--name", default="forge-xcode", help="the new image's name")
    prepare.add_argument("--wheels", type=Path, required=True,
                         help="a folder with the forge and forge-macworker wheels")  # fmt: skip
    run = commands.add_parser("run", help="take jobs from a Forge Web server")
    run.add_argument(
        "--server", required=True, help="the server's address, e.g. https://forge.example.com"
    )
    run.add_argument("--token-file", type=Path, help=f"a file with the token (else ${TOKEN_ENV})")
    run.add_argument("--image", default="forge-xcode", help="the prepared Tart image")
    direct = "no VMs: build on this Mac itself (only for projects you trust)"
    run.add_argument("--direct", action="store_true", help=direct)
    run.add_argument("--slots", type=int, default=2, choices=(1, 2), help="jobs at once (VMs)")
    run.add_argument("--network", choices=("softnet", "nat"), default="softnet",
                     help="softnet: VMs reach the internet but not this Mac's network")  # fmt: skip
    run.add_argument("--work", type=Path, default=Path.home() / ".forge-mac-worker",
                     help="where job and project folders go")  # fmt: skip
    return top


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "check":
        problems = check_setup(args.image, direct=args.direct)
        for line in problems or ["this Mac is ready"]:
            print(line)
        return 1 if problems else 0
    if args.command == "prepare-image":
        return asyncio.run(prepare_image(args.base, args.name, args.wheels))
    token = read_token(args.token_file)
    if not token:
        print(f"no token: pass --token-file or set {TOKEN_ENV}", file=sys.stderr)
        return 2
    return asyncio.run(serve(args, token))


def read_token(path: Path | None) -> str:
    """The worker token from the file or the environment."""
    if path is not None:
        return path.read_text("utf-8").strip()
    return os.environ.get(TOKEN_ENV, "").strip()


def make_runner(args: argparse.Namespace) -> Runner:
    """Direct mode, or VMs."""
    if args.direct:
        return DirectRunner(args.work)
    tart = shutil.which("tart") or "tart"
    settings = TartSettings(image=args.image, work=args.work / "vms", slots=args.slots,
                            network=args.network, tart=tart)  # fmt: skip
    return TartRunner(settings)


async def serve(args: argparse.Namespace, token: str) -> int:
    """Take jobs until Ctrl+C or SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    mode = "directly on this Mac" if args.direct else f"in VMs from {args.image}"
    logging.info("taking jobs from %s, %d at once, %s", args.server, args.slots, mode)
    client = WorkerClient(args.server, token, make_runner(args), slots=args.slots)
    await client.serve(stop)
    return 0
