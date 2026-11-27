"""Forge's WireGuard network between the nodes (W65): what the server sends a node about it.

Every node has an address in `10.90.0.0/24` on the interface `forge0` (Forge Web itself is
`10.90.0.1`; `10.89.0.0/24` is the build network). The poll answer carries a `mesh_revision`;
when it changes, the worker fetches its `MeshView` with `GET /api/host/mesh`: its own address,
its peers (public key, one address each, an endpoint where known) and the rules for traffic
that may come in on `forge0` (everything else is dropped). The private keys never travel: each
node keeps its own (`wg.key`), and Forge Web derives its own from its master key.

Everything here ends up in a file that `wg-quick` reads as root, so every value is checked
strictly: no value can carry a line break or a second setting.
"""

import hashlib
import ipaddress
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge_hostworker.wire import endpoint_ok, key32

MESH_SUBNET = ipaddress.ip_network("10.90.0.0/24")
FORGE_WEB_IP = "10.90.0.1"
INTERFACE = "forge0"
WG_PORT = 51820
KEEPALIVE_S = 25  # keeps a tunnel through NAT open
LABEL = r"^[a-z0-9-]{1,40}$"  # a peer's name in the file: a host id or "forge-web"
HOOKS = ("PostUp", "PostDown")
HEADER = "# Forge's WireGuard network, written by forge-host-worker; changes are overwritten."


def mesh_address(value: str) -> str:
    """A node's address: a host address of the network (ValueError otherwise)."""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError("not an IPv4 address") from None
    if address not in MESH_SUBNET or address in (
        MESH_SUBNET.network_address,
        MESH_SUBNET.broadcast_address,
    ):
        raise ValueError(f"not an address in {MESH_SUBNET}")
    return str(address)


class MeshPeer(BaseModel):
    """One other end of the tunnel: who, its key, the one address it may use, where it is."""

    model_config = ConfigDict(extra="forbid")
    label: str = Field(pattern=LABEL)
    public_key: str = Field(max_length=64)
    allowed_ip: str = Field(max_length=20)  # "10.90.0.7/32"
    endpoint: str | None = Field(default=None, max_length=300)
    keepalive_s: int = Field(default=KEEPALIVE_S, ge=0, le=3600)

    @field_validator("public_key")
    @classmethod
    def a_key(cls, value: str) -> str:
        """32 bytes of base64."""
        if not key32(value):
            raise ValueError("must be a key of 32 bytes in base64")
        return value

    @field_validator("allowed_ip")
    @classmethod
    def one_address(cls, value: str) -> str:
        """Exactly one address of the network."""
        address, slash, bits = value.partition("/")
        if slash != "/" or bits != "32":
            raise ValueError("must be one address, a.b.c.d/32")
        return f"{mesh_address(address)}/32"

    @field_validator("endpoint")
    @classmethod
    def an_endpoint(cls, value: str | None) -> str | None:
        """An address and a port."""
        if value is not None and not endpoint_ok(value):
            raise ValueError("must be IP:PORT, [IPv6]:PORT or a host name with :PORT")
        return value


class MeshRule(BaseModel):
    """Traffic that may come in on `forge0`: from this address, on these ports."""

    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=15)
    protocol: Literal["tcp", "udp"]
    port_from: int = Field(ge=1, le=65535)
    port_to: int = Field(ge=1, le=65535)

    @field_validator("source")
    @classmethod
    def in_the_network(cls, value: str) -> str:
        """An address of the network."""
        return mesh_address(value)

    @model_validator(mode="after")
    def a_range(self) -> "MeshRule":
        """The first port comes first."""
        if self.port_from > self.port_to:
            raise ValueError("port_from is after port_to")
        return self


class MeshView(BaseModel):
    """A node's part of the network (`GET /api/host/mesh`); no address: it is not in it (no
    WireGuard key, or no address free)."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(default=0, ge=0)
    address: str | None = Field(default=None, max_length=15)
    peers: list[MeshPeer] = Field(default_factory=list, max_length=254)
    rules: list[MeshRule] = Field(default_factory=list, max_length=2048)

    @field_validator("address")
    @classmethod
    def a_node_address(cls, value: str | None) -> str | None:
        """An address of the network other than Forge Web's."""
        if value is None:
            return None
        if mesh_address(value) == FORGE_WEB_IP:
            raise ValueError("that is Forge Web's own address")
        return value


def revision_of(view: MeshView) -> int:
    """A number that changes whenever anything in the view changes (never 0)."""
    content = view.model_dump_json(exclude={"revision"})
    return int(hashlib.sha256(content.encode()).hexdigest()[:15], 16) or 1


def wg_quick_config(
    private_key: str,
    address: str,
    peers: Sequence[MeshPeer],
    *,
    hooks: Sequence[tuple[str, str]] = (),
    header: str = HEADER,
) -> str:
    """The `wg-quick` file of `forge0` with this key, address and peers; ValueError for a key
    that is not one (the message never shows it)."""
    if not key32(private_key):
        raise ValueError("the WireGuard private key is not a key of 32 bytes in base64")
    lines = [header, "[Interface]", f"Address = {mesh_address(address)}/{MESH_SUBNET.prefixlen}",
             f"ListenPort = {WG_PORT}", f"PrivateKey = {private_key}"]  # fmt: skip
    for name, command in hooks:
        if name not in HOOKS or not command.isprintable() or not command.isascii():
            raise ValueError(f"not a hook: {name}")
        lines.append(f"{name} = {command}")
    for peer in peers:
        lines += ["", "[Peer]", f"# {peer.label}", f"PublicKey = {peer.public_key}",
                  f"AllowedIPs = {peer.allowed_ip}"]  # fmt: skip
        if peer.endpoint:
            lines.append(f"Endpoint = {peer.endpoint}")
        if peer.keepalive_s:
            lines.append(f"PersistentKeepalive = {peer.keepalive_s}")
    return "\n".join(lines) + "\n"
