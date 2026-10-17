"""`forge-win-worker`: test, package and sign Windows apps for a Forge Web server.

  forge-win-worker check
  forge-win-worker run --server URL --direct [--token-file FILE] [--slots N] [--work DIR]
  forge-win-worker run --server URL --sign-template "COMMAND {{file}}" [--token-file FILE]

The token comes from the admin page (Admin > Stores > Add a Windows machine): put it into a file
only you can read and pass `--token-file`, or set WIN_WORKER_TOKEN.

A build machine (`--direct`) builds the products' code on itself: give it a Windows VM of its
own that holds nothing else, with the .NET 10 SDK and Velopack's vpk
(`dotnet tool install -g vpk`), and reset it to a clean snapshot now and then.

A signing machine (`--sign-template`) only packs and signs apps the build machines published:
it never runs a product's code, so its signing key is safe there. The template is the command
Velopack runs for every file, with {{file}} where the file goes, e.g. signtool with Azure
Artifact Signing. Mark the machine as a signing machine on the admin page too.
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from forge_winworker import __version__
from forge_winworker.client import WorkerClient
from forge_winworker.runners import DirectRunner

TOKEN_ENV = "WIN_WORKER_TOKEN"  # not FORGE_*: Forge reads those as its own settings


def parser() -> argparse.ArgumentParser:
    """The command line."""
    top = argparse.ArgumentParser(prog="forge-win-worker", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    top.add_argument("--version", action="version", version=f"forge-win-worker {__version__}")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="is this machine ready to build?")
    run = commands.add_parser("run", help="take jobs from a Forge Web server")
    run.add_argument(
        "--server", required=True, help="the server's address, e.g. https://forge.example.com"
    )
    run.add_argument("--token-file", type=Path, help=f"a file with the token (else ${TOKEN_ENV})")
    run.add_argument("--direct", action="store_true",
                     help="build on this machine itself (a VM of its own, see above)")  # fmt: skip
    run.add_argument("--sign-template", default="",
                     help="a signing machine: its signing command, with {{file}}")  # fmt: skip
    run.add_argument("--slots", type=int, default=1, choices=(1, 2), help="jobs at once")
    run.add_argument("--work", type=Path, default=Path.home() / ".forge-win-worker",
                     help="where the job folders go")  # fmt: skip
    return top


def check_setup() -> list[str]:
    """What is missing on this machine ([] when it is ready)."""
    problems = []
    if os.name != "nt":
        problems.append("FAIL this is not Windows: WinUI 3 apps build on Windows only")
    if shutil.which("dotnet") is None:
        problems.append("FAIL dotnet is missing: install the .NET 10 SDK")
    if shutil.which("vpk") is None:
        problems.append("FAIL vpk is missing: dotnet tool install -g vpk --version 1.2.161")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "check":
        problems = check_setup()
        for line in problems or ["this machine is ready"]:
            print(line)
        return 1 if problems else 0
    problem = mode_problem(args.direct, args.sign_template)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    token = read_token(args.token_file)
    if not token:
        print(f"no token: pass --token-file or set {TOKEN_ENV}", file=sys.stderr)
        return 2
    return asyncio.run(serve(args, token))


def mode_problem(direct: bool, sign_template: str) -> str:
    """Why the worker cannot run like this ("" when it can): it builds or it signs."""
    if direct and sign_template:
        return "a signing machine never builds: pass --direct or --sign-template, not both"
    if sign_template and "{{file}}" not in sign_template:
        return "the signing command needs {{file}} where the file to sign goes"
    if not direct and not sign_template:
        return ("builds run on this machine itself: give the worker a VM of its own and pass "
                "--direct (or make it a signing machine with --sign-template)")  # fmt: skip
    return ""


def read_token(path: Path | None) -> str:
    """The worker token from the file or the environment (which jobs then never see)."""
    found = os.environ.pop(TOKEN_ENV, "").strip()
    if path is not None:
        return path.read_text("utf-8").strip()
    return found


async def serve(args: argparse.Namespace, token: str) -> int:
    """Take jobs until Ctrl+C (or SIGTERM where there is one)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows' event loop has no signal handlers
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    role = "signing" if args.sign_template else "build"
    logging.info("taking %s jobs from %s, %d at once", role, args.server, args.slots)
    client = WorkerClient(args.server, token, DirectRunner(args.work), slots=args.slots,
                          sign_template=args.sign_template)  # fmt: skip
    await client.serve(stop)
    return 0
