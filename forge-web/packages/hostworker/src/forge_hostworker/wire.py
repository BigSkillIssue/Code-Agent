"""What the server and a host worker send each other (the server imports only this module).

The worker asks for work with `POST /api/host/poll` and gets a `HostJob` (or none after a
while). A release job's packed source comes from `GET /api/host/jobs/<id>/source` and its
secrets, sealed to the worker's key, once from `GET /api/host/jobs/<id>/secrets`; the worker
reports with `POST /api/host/jobs/<id>/result`. Every call carries
`Authorization: Bearer <host token>`.

A `DeployPlan` is everything the worker needs, resolved by the server: fixed runtime images,
commands, ports, limits and the *names* of secrets. The worker never reads a product's
Dockerfile or compose file.
"""

import base64
import os
import re
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, Field, field_validator, model_validator

TOKEN_PREFIX = "fhw"
SLUG = r"^[a-z][a-z0-9-]{0,39}$"
ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
# A runtime image the admin built or pinned: registry/name:tag, optionally @sha256:digest.
IMAGE = r"^[a-z0-9][a-z0-9._/-]{0,199}(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?$"
RESERVED_ENV = frozenset({"PORT", "DATABASE_URL", "PATH", "HOME"})
MAX_LOG_CHARS = 20_000
SEAL_INFO = b"forge-hostworker secrets v1"
Runtime = Literal["python3.12", "node22", "static"]
Environment = Literal["staging", "production"]
JobKind = Literal["release", "rollback", "stop"]


class Limits(BaseModel):
    """What one app may use on the host, for each of its containers."""

    cpus: float = Field(default=1.0, gt=0, le=16)
    memory_mb: int = Field(default=512, ge=64, le=65536)
    pids: int = Field(default=256, ge=16, le=4096)


class ServicePlan(BaseModel):
    """One service of a release, resolved by the server from the product's forge.app.toml."""

    name: str = Field(pattern=SLUG)
    runtime: Runtime
    image: str = Field(pattern=IMAGE)
    root: str = Field(default=".", max_length=200)
    build: list[str] = Field(default_factory=list, max_length=50)
    command: list[str] = Field(default_factory=list, max_length=50)
    output: str = Field(default="dist", max_length=200)
    port: int = Field(ge=1024, le=65535)
    health: str = Field(default="/healthz", pattern=r"^/[\x21-\x7e]{0,199}$")
    route: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9/_-]{0,99}$")  # for the edge
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    secrets: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("root", "output")
    @classmethod
    def inside(cls, value: str) -> str:
        """A folder inside the release: relative, without `..`."""
        parts = value.replace("\\", "/").split("/")
        if value.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", value):
            raise ValueError("must be a folder inside the product")
        return value

    @field_validator("env")
    @classmethod
    def plain_names(cls, value: dict[str, str]) -> dict[str, str]:
        """Settings with proper names that hosting does not set itself."""
        for name in value:
            if not re.match(ENV_NAME, name) or name in RESERVED_ENV:
                raise ValueError(f"{name!r} cannot be set")
        return value

    @field_validator("secrets")
    @classmethod
    def secret_names(cls, value: list[str]) -> list[str]:
        """Secret names, never values."""
        for name in value:
            if not re.match(ENV_NAME, name) or name in RESERVED_ENV:
                raise ValueError(f"{name!r} is not a secret name")
        return value

    @model_validator(mode="after")
    def startable(self) -> "ServicePlan":
        """Python and Node services need a command; static ones are served by their image."""
        if self.runtime != "static" and not self.command:
            raise ValueError(f"a {self.runtime} service needs a command")
        return self


class DeployPlan(BaseModel):
    """One release of an app in one environment."""

    app: str = Field(pattern=SLUG)
    environment: Environment
    release: int = Field(ge=1, le=10**9)
    services: list[ServicePlan] = Field(min_length=1, max_length=8)
    database: bool = False
    limits: Limits = Field(default_factory=Limits)
    build_image_network: str = Field(default="forge-build", pattern=r"^[a-z][a-z0-9-]{0,62}$")

    @model_validator(mode="after")
    def distinct(self) -> "DeployPlan":
        """Service names and ports are used once."""
        if len({s.name for s in self.services}) != len(self.services):
            raise ValueError("service names must be distinct")
        if len({s.port for s in self.services}) != len(self.services):
            raise ValueError("service ports must be distinct")
        return self


class HostJob(BaseModel):
    """One job for the worker: run a release, go back to the previous one, or stop an app."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: JobKind
    app: str = Field(pattern=SLUG)
    environment: Environment
    plan: DeployPlan | None = None  # release jobs only
    timeout_s: float = Field(default=1800, gt=0, le=6 * 3600)

    @model_validator(mode="after")
    def plan_fits(self) -> "HostJob":
        """A release job carries the plan of the same app and environment; no other job does."""
        if (self.kind == "release") != (self.plan is not None):
            raise ValueError("a release job needs a plan, other jobs none")
        if self.plan is not None and (self.plan.app, self.plan.environment) != (
            self.app,
            self.environment,
        ):
            raise ValueError("the plan belongs to another app or environment")
        return self


class PollRequest(BaseModel):
    """The worker has room for more jobs."""

    free_slots: int = Field(ge=1, le=8)
    version: str = Field(default="", max_length=40)


class PollAnswer(BaseModel):
    """A job, or nothing yet (ask again)."""

    job: HostJob | None = None


class ServiceState(BaseModel):
    """A running service after a job."""

    name: str = Field(pattern=SLUG)
    container: str = Field(max_length=200)
    healthy: bool


class JobResult(BaseModel):
    """How a job ended. `ok` is False when the release is not live: it could not be built, did
    not get healthy (then the previous release keeps running: `rolled_back`), or the job could
    not run at all."""

    ok: bool
    release: int = Field(default=0, ge=0)  # the release now live (0: none)
    services: list[ServiceState] = Field(default_factory=list, max_length=8)
    rolled_back: bool = False
    error: str = Field(default="", max_length=2_000)
    hint: str = Field(default="", max_length=2_000)
    log_tail: str = Field(default="", max_length=MAX_LOG_CHARS)
    seconds: float = Field(default=0, ge=0)


class SealedSecrets(BaseModel):
    """A release's secrets, sealed to one host's X25519 key (only that worker can open them)."""

    ephemeral: str = Field(max_length=100)  # the sender's one-time public key, base64
    nonce: str = Field(max_length=40)
    sealed: str = Field(max_length=200_000)


def public_key(private: X25519PrivateKey) -> str:
    """A host's public key as base64 (registered with the server)."""
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _key(shared: bytes, ephemeral: bytes, recipient: bytes) -> bytes:
    """The symmetric key for one sealing."""
    return HKDF(hashes.SHA256(), 32, salt=ephemeral + recipient, info=SEAL_INFO).derive(shared)


def seal(recipient_b64: str, data: bytes) -> SealedSecrets:
    """Seal data to a host's public key (the server does this once per deploy)."""
    recipient = base64.b64decode(recipient_b64)
    one_time = X25519PrivateKey.generate()
    ephemeral = one_time.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = one_time.exchange(X25519PublicKey.from_public_bytes(recipient))
    nonce = os.urandom(12)
    box = ChaCha20Poly1305(_key(shared, ephemeral, recipient))
    sealed = box.encrypt(nonce, data, None)
    return SealedSecrets(
        ephemeral=base64.b64encode(ephemeral).decode(),
        nonce=base64.b64encode(nonce).decode(),
        sealed=base64.b64encode(sealed).decode(),
    )


def open_sealed(private: X25519PrivateKey, box: SealedSecrets) -> bytes:
    """Open sealed data with the host's private key; a wrong key or a change raises."""
    ephemeral = base64.b64decode(box.ephemeral)
    recipient = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral))
    cipher = ChaCha20Poly1305(_key(shared, ephemeral, recipient))
    return cipher.decrypt(base64.b64decode(box.nonce), base64.b64decode(box.sealed), None)
