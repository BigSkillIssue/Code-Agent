"""Forge's WireGuard network between the servers (W65).

Every enabled node with a WireGuard key gets an address in `10.90.0.0/24` the first time anyone
asks for the network (Forge Web is `.1`); the lowest free one, never twice (a unique index in the
database), and it is free again once the node goes (`release_address`). Who may talk to whom is a
fixed table, `mesh_policy()`: apps reach the databases, Forge Web reaches the AI models, storage
accepts nothing, nobody else talks to anybody. A node's view names only the peers the table
relates it to, and the rules for what may come in on its `forge0`.

Forge Web's own key pair is derived from its master key, so it is never stored; it changes only
when the master key does (then every view's revision changes and the nodes follow).
`forge-web mesh config` prints Forge Web's own `wg-quick` file.
"""

import asyncio
import base64
import logging
import sys
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from forge_hostworker.mesh_wire import (
    FORGE_WEB_IP,
    INTERFACE,
    MESH_SUBNET,
    WG_PORT,
    MeshPeer,
    MeshRule,
    MeshView,
    revision_of,
    wg_quick_config,
)
from forge_hostworker.wire import Role, endpoint_ok
from forge_web.audit import audit
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostServer
from forge_web.hosting.hosts import roles_of
from forge_web.settings import WebSettings
from forge_web.vault import Vault, load_master_key

FORGE_WEB = "forge-web"  # Forge Web's label, and a source in the policy
KEY_PURPOSE = "wireguard mesh"
TRIES = MESH_SUBNET.num_addresses  # each failed try means another node got an address
DROP_NEW = f"-i {INTERFACE} -m conntrack --ctstate NEW -j DROP"
HEADER = "# Forge's WireGuard network on Forge Web's own host, from `forge-web mesh config`."
log = logging.getLogger(__name__)
Source = Literal["forge-web", "apps", "db", "ai", "storage"]


@dataclass(frozen=True)
class PolicyRule:
    """Nodes of one role (or Forge Web) may reach nodes of another role on these ports."""

    source: Source
    target: Role
    protocol: Literal["tcp", "udp"]
    port_from: int
    port_to: int
    why: str


POLICY = (
    PolicyRule("apps", "db", "tcp", 5432, 5532, "the apps' Postgres (W68 narrows it per app)"),
    PolicyRule("forge-web", "ai", "tcp", 11434, 11434, "Ollama on the AI nodes (W69)"),
    PolicyRule("forge-web", "ai", "tcp", 8000, 8000, "vLLM on the AI nodes (W69)"),
)


def mesh_policy() -> tuple[PolicyRule, ...]:
    """Who may talk to whom in the network; nothing else passes (storage accepts nothing)."""
    return POLICY


def free_address(taken: set[str]) -> str | None:
    """The lowest address of the network no node has (None: the network is full)."""
    for address in MESH_SUBNET.hosts():
        if str(address) != FORGE_WEB_IP and str(address) not in taken:
            return str(address)
    return None


async def assign_address(db: Database, host_id: str) -> str | None:
    """The node's address, given now if it has none (None: no WireGuard key, or none free)."""
    for _ in range(TRIES):
        async with db.session() as session:
            row = await session.get(HostServer, host_id)
            if row is None or not row.wg_public_key:
                return None
            if row.mesh_ip:
                return row.mesh_ip
            taken = set(await session.scalars(
                select(HostServer.mesh_ip).where(HostServer.mesh_ip.is_not(None))))  # fmt: skip
        address = free_address({str(a) for a in taken})
        if address is None:
            log.warning("Forge's WireGuard network is full; node %s gets no address", host_id)
            return None
        try:
            async with db.session() as session, session.begin():
                await session.execute(update(HostServer).where(
                    HostServer.id == host_id, HostServer.mesh_ip.is_(None)
                ).values(mesh_ip=address))  # fmt: skip
        except IntegrityError:  # another node took it in the meantime: look again
            continue
    async with db.session() as session:
        row = await session.get(HostServer, host_id)
    return row.mesh_ip if row else None


async def release_address(db: Database, host_id: str) -> None:
    """The node leaves the network: its address is free for the next one."""
    async with db.session() as session, session.begin():
        await session.execute(
            update(HostServer).where(HostServer.id == host_id).values(mesh_ip=None)
        )


async def members(db: Database) -> list[HostServer]:
    """Every enabled node with a WireGuard key, each with its address (given where missing)."""
    async with db.session() as session:
        rows = list(await session.scalars(
            select(HostServer).where(HostServer.enabled, HostServer.wg_public_key.is_not(None))
            .order_by(HostServer.created_at, HostServer.id)))  # fmt: skip
    for row in rows:
        if not row.mesh_ip:
            row.mesh_ip = await assign_address(db, row.id)
    return [row for row in rows if row.mesh_ip]


def forge_web_keys(vault: Vault) -> tuple[str, str]:
    """Forge Web's WireGuard private and public key (base64), derived from its master key."""
    raw = bytearray(vault.derive(KEY_PURPOSE))
    raw[0] &= 248  # clamped as `wg genkey` makes keys
    raw[31] = (raw[31] & 127) | 64
    private = X25519PrivateKey.from_private_bytes(bytes(raw))
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(bytes(raw)).decode(), base64.b64encode(public).decode()


def forge_web_endpoint(settings: WebSettings) -> str | None:
    """Where nodes reach Forge Web's WireGuard: its public host name, port 51820 (None: no
    public address; then Forge Web reaches the nodes that name an endpoint)."""
    host = urlsplit(settings.server.public_url).hostname if settings.server.public_url else None
    if not host or host == "localhost":
        return None
    with_port = f"[{host}]:{WG_PORT}" if ":" in host else f"{host}:{WG_PORT}"
    return with_port if endpoint_ok(with_port) else None


def related(policy: PolicyRule, source: list[Role], target: list[Role]) -> bool:
    """Whether the rule lets a node with the source roles reach one with the target roles."""
    return policy.source in source and policy.target in target


def rules_for(host: HostServer, everyone: list[HostServer]) -> list[MeshRule]:
    """What may come in on this node's forge0, by the policy."""
    mine, rules = roles_of(host), []
    for policy in POLICY:
        if policy.target not in mine:
            continue
        sources = [FORGE_WEB_IP] if policy.source == FORGE_WEB else [
            str(other.mesh_ip) for other in everyone if other.id != host.id and other.enabled
            and other.mesh_ip and policy.source in roles_of(other)]  # fmt: skip
        rules += [MeshRule(source=source, protocol=policy.protocol, port_from=policy.port_from,
                           port_to=policy.port_to) for source in sources]  # fmt: skip
    return rules


def peers_for(
    host: HostServer, everyone: list[HostServer], forge_public: str, forge_endpoint: str | None
) -> list[MeshPeer]:
    """Forge Web and the nodes the policy relates this one to (either way)."""
    peers = [MeshPeer(label=FORGE_WEB, public_key=forge_public,
                      allowed_ip=f"{FORGE_WEB_IP}/32", endpoint=forge_endpoint)]  # fmt: skip
    mine = roles_of(host)
    for other in everyone:
        if other.id == host.id or not other.enabled or not other.mesh_ip:
            continue
        theirs = roles_of(other)
        if other.wg_public_key and any(
            related(p, mine, theirs) or related(p, theirs, mine) for p in POLICY
        ):
            peers.append(MeshPeer(label=other.id, public_key=other.wg_public_key,
                                  allowed_ip=f"{other.mesh_ip}/32",
                                  endpoint=other.endpoint))  # fmt: skip
    return peers


async def node_view(
    db: Database, vault: Vault, settings: WebSettings, host: HostServer
) -> MeshView:
    """The node's part of the network, with its revision (no address: not in the network)."""
    if not host.wg_public_key or not host.enabled:
        return MeshView()
    everyone = await members(db)
    me = next((row for row in everyone if row.id == host.id), None)
    if me is None:
        return MeshView()
    _, public = forge_web_keys(vault)
    view = MeshView(address=me.mesh_ip, peers=peers_for(me, everyone, public,
                    forge_web_endpoint(settings)), rules=rules_for(me, everyone))  # fmt: skip
    view.revision = revision_of(view)
    return view


async def forge_web_config(db: Database, vault: Vault, settings: WebSettings) -> str:
    """Forge Web's own `wg-quick` file: its address, its key, every node as a peer; nothing
    new may come in on forge0 (Forge Web only ever starts the connections)."""
    private, _ = forge_web_keys(vault)
    peers = [MeshPeer(label=row.id, public_key=str(row.wg_public_key),
                      allowed_ip=f"{row.mesh_ip}/32", endpoint=row.endpoint)
             for row in await members(db)]  # fmt: skip
    hooks = [(name, f"iptables -{flag} {chain} {DROP_NEW}")
             for name, flag in (("PostUp", "I"), ("PostDown", "D"))
             for chain in ("INPUT", "FORWARD")]  # fmt: skip
    return wg_quick_config(private, FORGE_WEB_IP, peers, hooks=hooks, header=HEADER)


def config_command(settings: WebSettings) -> int:
    """`forge-web mesh config`: print Forge Web's own `wg-quick` file (it holds the key)."""
    return asyncio.run(_config_command(settings))


async def _config_command(settings: WebSettings) -> int:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url())
    await db.migrate()
    try:
        text = await forge_web_config(db, Vault(load_master_key(settings.data_dir)), settings)
        await audit(db, "hosting.mesh_config_shown", peers=text.count("[Peer]"))
    finally:
        await db.close()
    sys.stdout.write(text)
    return 0
