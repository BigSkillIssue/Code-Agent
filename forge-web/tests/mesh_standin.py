"""Helpers of the WireGuard network's tests (W65): nodes, views and `wg`/`wg-quick`/`iptables`
without WireGuard."""

import base64
import hashlib
import json
from pathlib import Path

import httpx
from sqlalchemy import select

from forge_hostworker.mesh_wire import MeshPeer
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostServer
from forge_web.hosting.hosts import create_host
from host_standin import HostStandIn
from support import LiveServer


def key32(seed: str) -> str:
    """A base64 key of 32 bytes (as `wg genkey` and `wg pubkey` print them)."""
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


async def node(
    db: Database, name: str, roles: list[str], endpoint: str | None = None, wg: bool = True
) -> tuple[HostServer, str]:
    """A host with a WireGuard key (unless `wg` is off), as `join` makes one."""
    return await create_host(db, name, HostStandIn().public_key, roles,
                             wg_public_key=key32(name) if wg else None,
                             endpoint=endpoint)  # fmt: skip


async def stored(db: Database, host_id: str) -> HostServer:
    async with db.session() as session:
        row = await session.get(HostServer, host_id)
    assert row is not None
    return row


def member(host_id: str, roles: list[str], address: str) -> HostServer:
    """A host in the network, not stored."""
    return HostServer(id=host_id, name=host_id, roles=json.dumps(roles), mesh_ip=address,
                      wg_public_key=key32(host_id), endpoint=f"203.0.113.{address[-1]}:51820",
                      enabled=True)  # fmt: skip


def peer(label: str, address: str, endpoint: str | None = None) -> MeshPeer:
    return MeshPeer(label=label, public_key=key32(label), allowed_ip=f"{address}/32",
                    endpoint=endpoint)  # fmt: skip


class FakeWireGuard:
    """`wg`, `wg-quick` and `iptables` without WireGuard: records every command."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.calls: list[list[str]] = []
        self.up = False
        self.fail: str = ""  # a command that fails
        self.synced = ""  # what `wg syncconf` was given
        self.synced_mode = 0

    def __call__(self, argv: list[str], stdin: str) -> tuple[int, str]:
        self.calls.append(argv)
        if self.fail and " ".join(argv).startswith(self.fail):
            return 1, ""
        if argv[:2] == ["wg", "show"]:
            return (0 if self.up else 1), ""
        if argv[:2] == ["wg-quick", "up"]:
            assert argv[2] == str(self.config) and self.config.is_file()
            self.up = True
        if argv[:2] == ["wg-quick", "down"]:
            self.up = False
        if argv[:2] == ["wg-quick", "strip"]:
            return 0, "\n".join(line for line in self.config.read_text().splitlines()
                                if not line.startswith(("Address", "#")))  # fmt: skip
        if argv[:2] == ["wg", "syncconf"]:
            path = Path(argv[3])
            self.synced, self.synced_mode = path.read_text(), path.stat().st_mode & 0o777
        if argv[:2] == ["iptables", "-C"]:
            return 1, ""
        return 0, ""

    def commands(self) -> list[str]:
        return [" ".join(argv[:2]) for argv in self.calls]


def worker_data(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "node"
    data.mkdir()
    (data / "wg.key").write_text(key32("node-private") + "\n", encoding="utf-8")
    return data, tmp_path / "wireguard" / "forge0.conf"


def mesh_client(server: LiveServer, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, headers={"Authorization": f"Bearer {token}"},
                             timeout=30)  # fmt: skip


async def revision(http: httpx.AsyncClient) -> int:
    reply = await http.post("/api/host/poll", json={"free_slots": 1})
    assert reply.status_code == 200, reply.text
    return int(reply.json()["mesh_revision"])


async def host_named(db: Database, name: str) -> HostServer:
    async with db.session() as session:
        row = await session.scalar(select(HostServer).where(HostServer.name == name))
    assert row is not None
    return row


def labels(view: dict[str, object]) -> list[str]:
    return [str(p["label"]) for p in view["peers"]]  # type: ignore[attr-defined]
