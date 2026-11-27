"""One-time join codes for new servers (W63).

An admin makes a code `fjn_<id>_<secret>` with the new server's name and roles; it is shown once
and only the SHA-256 of its secret part is kept. Within 24 hours the server trades it, once, with
`forge-host-worker join` for its host token, sending its X25519 key (secrets are sealed to it)
and its WireGuard key. Every reason a code does not work (garbled, unknown, wrong, used, expired)
gives the same answer, so a guess learns nothing; the route also limits attempts per address.
"""

import hmac
import json
import secrets
import time
from collections.abc import Iterable

from sqlalchemy import update

from forge_hostworker.wire import JOIN_PREFIX, JoinRequest
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostJoinCode, HostServer
from forge_web.hosting.hosts import create_host, secret_hash, valid_roles

JOIN_TTL_S = 24 * 3600
REFUSED = "this join code is not valid (wrong, used or expired); an admin makes a new one"
NO_SECRET = secret_hash("")  # compared with when there is no such code, so it takes as long

__all__ = ["JOIN_TTL_S", "JoinRefused", "create_join_code", "redeem", "valid_roles"]


class JoinRefused(Exception):
    """The code does not work; the message never says why."""


async def create_join_code(
    db: Database, name: str, roles: Iterable[str], *, created_by: str
) -> tuple[HostJoinCode, str]:
    """A new code for a server with this name and these roles, and the code itself (shown
    once, not stored); ValueError for roles that are not right."""
    kept = valid_roles(roles)
    code_id, secret, now = secrets.token_hex(8), secrets.token_urlsafe(32), time.time()
    row = HostJoinCode(id=code_id, name=name, roles=json.dumps(kept), code_hash=secret_hash(secret),
                       created_by=created_by, created_at=now, expires_at=now + JOIN_TTL_S,
                       used_at=0.0)  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(row)
    return row, f"{JOIN_PREFIX}_{code_id}_{secret}"


def parse_code(code: str) -> tuple[str, str] | None:
    """The id and the secret of a code that looks like one, else None."""
    parts = code.split("_", 2)
    if len(parts) != 3 or parts[0] != JOIN_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


async def redeem(db: Database, body: JoinRequest) -> tuple[HostServer, str]:
    """Spend the code: the new host with the code's name and roles, and its token (shown
    once). JoinRefused when the code does not work."""
    parsed = parse_code(body.code)
    if parsed is None:
        raise JoinRefused(REFUSED)
    code_id, secret = parsed
    now = time.time()
    async with db.session() as session, session.begin():
        row = await session.get(HostJoinCode, code_id)
        matches = hmac.compare_digest(row.code_hash if row else NO_SECRET, secret_hash(secret))
        if row is None or not matches or row.used_at or row.expires_at <= now:
            raise JoinRefused(REFUSED)
        spent = await session.execute(
            update(HostJoinCode).where(HostJoinCode.id == code_id, HostJoinCode.used_at == 0)
            .values(used_at=now)
        )  # fmt: skip
        if spent.rowcount != 1:  # type: ignore[attr-defined]  # another request was first
            raise JoinRefused(REFUSED)
        name, roles = row.name, json.loads(row.roles)
    host, token = await create_host(
        db, name, body.public_key, roles, wg_public_key=body.wg_public_key, endpoint=body.endpoint
    )
    async with db.session() as session, session.begin():
        await session.execute(
            update(HostJoinCode).where(HostJoinCode.id == code_id).values(host_id=host.id)
        )
    return host, token
