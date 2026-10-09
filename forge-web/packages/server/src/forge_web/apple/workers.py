"""Mac workers: their tokens (shown once, stored as a hash) and how a request proves to be one."""

import hashlib
import hmac
import secrets
import time

from sqlalchemy import select

from forge_macworker.wire import TOKEN_PREFIX
from forge_web.db.engine import Database
from forge_web.db.models import MacWorker

SEEN_EVERY_S = 30  # last_seen is written at most this often
ONLINE_S = 120  # a worker seen this recently counts as connected


def secret_hash(secret: str) -> str:
    """What is stored instead of the token's secret part."""
    return hashlib.sha256(secret.encode()).hexdigest()


async def create_worker(db: Database, name: str) -> tuple[MacWorker, str]:
    """A new worker and its token (`fmw_<id>_<secret>`); the token is not stored."""
    worker_id, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
    row = MacWorker(id=worker_id, name=name, token_hash=secret_hash(secret), enabled=True,
                    created_at=time.time())  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(row)
    return row, f"{TOKEN_PREFIX}_{worker_id}_{secret}"


async def worker_of(db: Database, authorization: str, version: str = "") -> MacWorker | None:
    """The enabled worker whose token the header carries, else None (and mark it seen)."""
    if not authorization.lower().startswith("bearer "):
        return None
    parts = authorization[7:].strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    async with db.session() as session, session.begin():
        row = await session.get(MacWorker, parts[1])
        if row is None or not hmac.compare_digest(row.token_hash, secret_hash(parts[2])):
            return None
        if not row.enabled:
            return None
        now = time.time()
        if now - row.last_seen > SEEN_EVERY_S or (version and version != row.version):
            row.last_seen = now
            row.version = version or row.version
    return row


async def any_online(db: Database) -> bool:
    """True when an enabled worker asked for work recently."""
    since = time.time() - ONLINE_S
    async with db.session() as session:
        found = await session.scalar(
            select(MacWorker.id).where(MacWorker.enabled, MacWorker.last_seen >= since).limit(1)
        )
    return found is not None
