"""`forge-host-worker`: run a Linux server's apps for a Forge Web server.

    forge-host-worker join --server URL --code fjn_... --data DIR [--endpoint IP:PORT]
                          make both keys (once), trade the admin's join code for the token
    forge-host-worker keygen --data DIR       make the host's key (once); prints the public key
    forge-host-worker doctor --data DIR       is this host ready? names every missing piece
    forge-host-worker firewall [--apply] [--allow ADDR ...]   print or (root) load the rules
    forge-host-worker run --server URL --data DIR [--token-file FILE] [--slots N] [--docker PATH]
                          [--apps-domain DOMAIN --legal-base URL]   (with Caddy as the edge)
                          [--wg-config /etc/wireguard/forge0.conf]  (Forge's WireGuard network)

The token comes from --token-file (default DIR/token, where `join` writes it) or
FORGE_HOST_TOKEN; without `join`, the admin makes it in Forge Web and registers the public key.
"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from forge_hostworker import __version__, firewall
from forge_hostworker.client import HostClient
from forge_hostworker.docker import DockerCli, HostError, container_owner, require_runsc
from forge_hostworker.doctor import TEST_IMAGE, doctor, is_root, report
from forge_hostworker.downloads import DOWNLOADS, Downloads
from forge_hostworker.edge import CADDY_DATA, Edge
from forge_hostworker.mesh import CONFIG, WG_KEY, MeshNode
from forge_hostworker.runner import HostRunner, default_user
from forge_hostworker.wire import (
    JOIN_PREFIX,
    TOKEN_PREFIX,
    JoinAnswer,
    JoinRequest,
    key32,
    public_key,
)

KEY = "host.key"
TOKEN = "token"
Capture = Callable[[list[str], str], str]  # argv, stdin -> what it printed; OSError on failure


def main(argv: list[str] | None = None) -> int:
    """Run the command line; the exit code."""
    parser = argparse.ArgumentParser(
        prog="forge-host-worker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"forge-host-worker {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    add_join(commands)
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
    run.add_argument(
        "--devices",
        action="store_true",
        help="run Android emulators for Forge's agent (needs /dev/kvm)",
    )
    run.add_argument("--docker", default="docker")
    run.add_argument("--apps-domain", help="apps answer at <app>.DOMAIN (Caddy is the edge)")
    run.add_argument("--legal-base", help="Forge Web's legal pages, https://forge.../legal")
    run.add_argument("--caddy-data", type=Path, default=CADDY_DATA,
                     help="Caddy's storage, to remove deleted apps' certificates")  # fmt: skip
    run.add_argument("--downloads", type=Path, default=DOWNLOADS,
                     help="where the Windows apps' downloads go (Caddy must read it)")  # fmt: skip
    run.add_argument("--wg-config", type=Path, default=CONFIG,
                     help="the wg-quick file of Forge's network (forge0.conf)")  # fmt: skip
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "join":
        return asyncio.run(join(args.server, args.code, args.data, args.endpoint, wg=wg_command))
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


def add_join(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """The `join` command's arguments."""
    joining = commands.add_parser("join", help="join Forge Web with an admin's one-time code")
    joining.add_argument("--server", required=True, help="Forge Web's URL, https://...")
    joining.add_argument("--code", required=True, help="the join code, fjn_...")
    joining.add_argument("--data", type=Path, required=True, help="where releases and keys live")
    joining.add_argument("--endpoint", help="where the other servers reach this one, IP:PORT")


def make_key(data: Path) -> int:
    """Make the host's X25519 key once; print its public half."""
    path = data / KEY
    if path.exists():
        print(f"{path} exists; its public key: {public_key(load_key(path))}")
        return 0
    private = ensure_key(data)
    print(f"made {path}; register this public key in Forge Web: {public_key(private)}")
    return 0


def ensure_key(data: Path) -> X25519PrivateKey:
    """The host's X25519 key, made once (0600)."""
    path = data / KEY
    if path.exists():
        return load_key(path)
    data.mkdir(parents=True, exist_ok=True)
    private = X25519PrivateKey.generate()
    path.touch(mode=0o600)
    path.write_bytes(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    return private


def load_key(path: Path) -> X25519PrivateKey:
    """The host's private key."""
    return X25519PrivateKey.from_private_bytes(path.read_bytes())


def private_file(path: Path, text: str) -> None:
    """Write a file only its owner may read (0600, set before anything is in it)."""
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")


def wg_command(argv: list[str], stdin: str) -> str:
    """Run a `wg` command with this input; what it printed (OSError when it fails)."""
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False,
                              timeout=30)  # fmt: skip
    except FileNotFoundError:
        raise OSError("`wg` is missing; install wireguard-tools") from None
    except subprocess.TimeoutExpired:
        raise OSError(f"`{' '.join(argv)}` did not finish") from None
    if done.returncode != 0:
        raise OSError(f"`{' '.join(argv)}` failed: {done.stderr.strip()[:500]}")
    return done.stdout


def wireguard_key(data: Path, wg: Capture) -> str:
    """The host's WireGuard public key; the private key is made once (`wg genkey`, 0600)."""
    path = data / WG_KEY
    if not path.is_file():
        private_file(path, wg(["wg", "genkey"], "").strip() + "\n")
    public = wg(["wg", "pubkey"], path.read_text(encoding="utf-8")).strip()
    if not key32(public):
        raise OSError("`wg pubkey` printed no key")
    return public


def failed(message: str) -> int:
    """Say what went wrong; the exit code."""
    print(f"error: {message}", file=sys.stderr)
    return 2


async def join(
    server: str,
    code: str,
    data: Path,
    endpoint: str | None = None,
    *,
    wg: Capture = wg_command,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Make both keys (once), trade the one-time join code for this host's token and write it
    to DATA/token (0600); the exit code."""
    if not code.startswith(f"{JOIN_PREFIX}_"):
        return failed(f"a join code starts with {JOIN_PREFIX}_; an admin makes one in Forge Web")
    if (data / TOKEN).exists():
        return failed(f"{data / TOKEN} exists: this host has joined (remove it to join again)")
    key = ensure_key(data)
    try:
        wg_public = await asyncio.to_thread(wireguard_key, data, wg)
        body = JoinRequest(code=code, public_key=public_key(key), wg_public_key=wg_public,
                           endpoint=endpoint)  # fmt: skip
    except (OSError, ValueError) as error:
        return failed(str(error))
    async with httpx.AsyncClient(base_url=server.rstrip("/"), transport=transport, timeout=60,
                                 headers={"X-Forge-Host-Worker": __version__}) as http:  # fmt: skip
        try:
            reply = await http.post("/api/host/join", content=body.model_dump_json(),
                                    headers={"Content-Type": "application/json"})  # fmt: skip
        except httpx.HTTPError as error:
            return failed(f"Forge Web did not answer: {error}")
    if reply.status_code != 200:
        return failed(f"Forge Web refused ({reply.status_code}): {detail(reply)}")
    answer = JoinAnswer.model_validate_json(reply.content)
    private_file(data / TOKEN, answer.token + "\n")
    print(
        f"joined as {answer.name} ({answer.host_id}), roles: {', '.join(answer.roles)}; "
        f"next: forge-host-worker run --server {server} --data {data}"
    )
    return 0


def detail(reply: httpx.Response) -> str:
    """The reason in a refusal."""
    try:
        said = reply.json()
    except ValueError:
        said = None
    return str(said.get("detail") if isinstance(said, dict) else reply.text)[:500]


def read_token(args: argparse.Namespace) -> str:
    """The host token from its file or the environment; '' when there is none."""
    path: Path = args.token_file or args.data / TOKEN
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
    client = HostClient(
        args.server,
        token,
        runner,
        load_key(args.data / KEY),
        slots=args.slots,
        devices=args.devices,
        mesh=MeshNode(args.data, config=args.wg_config),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.apps_domain is None:
        await client.serve(stop)
        return 0
    edge = Edge(runner, docker, args.apps_domain, args.legal_base, certificates=args.caddy_data,
                downloads=Downloads(client.http, args.downloads))  # fmt: skip
    runner.on_change = edge.changed
    ask = await edge.serve_ask()
    try:
        await asyncio.gather(client.serve(stop), edge.keep_fresh(stop))
    finally:
        ask.close()
        await edge.close()
    return 0
