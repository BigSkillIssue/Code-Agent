"""This node's side of Forge's WireGuard network (W65).

When the poll answer's `mesh_revision` changes, the worker fetches its view, loads the firewall
chain for `forge0` (before the interface can come up), writes `/etc/wireguard/forge0.conf`
(0600, with the private key from `wg.key` that `join` made) and brings `forge0` up, or, when it
is up, hands the new peers to `wg syncconf` (the stripped file goes through a private temporary
file, not bash's process substitution). A new address takes the interface down and up again.
Every command goes through an injectable runner, so tests need no WireGuard. A failure is
logged without any key and tried again after a while.
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from forge_hostworker import firewall
from forge_hostworker.mesh_wire import INTERFACE, MESH_SUBNET, MeshView, wg_quick_config

WG_KEY = "wg.key"  # the node's WireGuard private key, made by `join` (0600)
CONFIG = Path("/etc/wireguard") / f"{INTERFACE}.conf"
RETRY_S = 300.0  # a view that could not be applied is tried again after this long
Capture = Callable[[list[str], str], tuple[int, str]]  # argv, stdin -> exit code, stdout
log = logging.getLogger(__name__)


class MeshError(OSError):
    """WireGuard could not be set up; the message never carries a key."""


def node_config(view: MeshView, private_key: str) -> str:
    """The `wg-quick` file of this node for its view."""
    if view.address is None:
        raise ValueError("this node has no address in Forge's network")
    return wg_quick_config(private_key, view.address, view.peers)


def capture(argv: list[str], stdin: str) -> tuple[int, str]:
    """Run a command (as root) with this input; its exit code and what it printed."""
    try:
        done = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, text=True,
                              check=False, timeout=60)  # fmt: skip
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout


def address_in(path: Path) -> str | None:
    """The address in an existing `forge0.conf` (None: no file, no address)."""
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "Address":
                return value.strip()
    return None


def write_private(path: Path, text: str) -> None:
    """Replace a file with this text, readable by its owner only from the first byte on."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


class MeshNode:
    """Keeps `forge0` as the server's newest view says."""

    def __init__(
        self,
        data_dir: Path,
        *,
        config: Path = CONFIG,
        run: Capture = capture,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.data_dir = data_dir
        self.config = config
        self.run = run
        self.clock = clock
        self.applied = 0  # the revision that is in place
        self.failed = 0  # the revision that could not be applied, and when to try it again
        self.retry_at = 0.0

    async def sync(self, http: httpx.AsyncClient, revision: int) -> bool:
        """Fetch and apply the view when the revision is new; whether it applied one."""
        if not revision or revision == self.applied:
            return False
        if revision == self.failed and self.clock() < self.retry_at:
            return False
        try:
            reply = await http.get("/api/host/mesh")
            reply.raise_for_status()
            view = MeshView.model_validate_json(reply.content)
            await asyncio.to_thread(self.apply, view)
        except (httpx.HTTPError, ValueError, OSError) as err:
            log.warning("Forge's WireGuard network could not be set up (again in %.0f s): %s",
                        RETRY_S, err)  # fmt: skip
            self.failed, self.retry_at = revision, self.clock() + RETRY_S
            return False
        self.applied = revision
        log.info("Forge's WireGuard network: %s with %d peers", view.address, len(view.peers))
        return True

    def apply(self, view: MeshView) -> list[str]:
        """Put the view in place (blocking); the commands it ran. Nothing without an address."""
        if view.address is None:
            return []
        text = node_config(view, self.private_key())
        done = firewall.apply_mesh(view.rules, run=lambda argv, stdin: self.run(argv, stdin)[0])
        up = self.must(["wg", "show", INTERFACE], may_fail=True) == 0
        wanted = f"{view.address}/{MESH_SUBNET.prefixlen}"
        if up and address_in(self.config) != wanted:
            self.must(["wg-quick", "down", str(self.config)])
            done.append("wg-quick down")
            up = False
        write_private(self.config, text)
        if not up:
            self.must(["wg-quick", "up", str(self.config)])
            return [*done, "wg-quick up"]
        self.syncconf()
        return [*done, "wg syncconf"]

    def syncconf(self) -> None:
        """`wg syncconf forge0 <(wg-quick strip forge0)`, through a private temporary file."""
        code, stripped = self.run(["wg-quick", "strip", str(self.config)], "")
        if code != 0:
            raise MeshError(f"`wg-quick strip` failed (exit {code})")
        handle, name = tempfile.mkstemp(prefix="forge0.strip.")  # 0600 from the start
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(stripped)
            self.must(["wg", "syncconf", INTERFACE, name])
        finally:
            Path(name).unlink(missing_ok=True)

    def must(self, argv: list[str], *, may_fail: bool = False) -> int:
        """Run a command; MeshError when it fails (unless it may), naming only the command."""
        code, _ = self.run(argv, "")
        if code != 0 and not may_fail:
            raise MeshError(f"`{' '.join(argv[:2])}` failed (exit {code})")
        return code

    def private_key(self) -> str:
        """The node's WireGuard private key from `wg.key`."""
        path = self.data_dir / WG_KEY
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            raise MeshError(
                f"no WireGuard key in {path}; this node joined without one "
                "(`forge-host-worker join` makes it)"
            ) from None
