"""Windows workers (W38a, W39): build and signing machines, their tokens (shown once, stored as
a hash, like the Macs') and how a request proves to be one."""

import hmac
import secrets
import time
from typing import Any

from forge_web.apple.workers import ONLINE_S, SEEN_EVERY_S, secret_hash
from forge_web.db.engine import Database
from forge_web.db.store_models import WinWorker
from forge_winworker.wire import TOKEN_PREFIX


def worker_view(row: WinWorker) -> dict[str, Any]:
    """A worker as the admin page shows it (never its token)."""
    return {"id": row.id, "name": row.name, "enabled": row.enabled, "signs": row.signs,
            "version": row.version,
            "last_seen": row.last_seen, "online": time.time() - row.last_seen < ONLINE_S,
            "created_at": row.created_at}  # fmt: skip


async def create_worker(db: Database, name: str, signs: bool = False) -> tuple[WinWorker, str]:
    """A new worker and its token (`fww_<id>_<secret>`); the token is not stored."""
    worker_id, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
    row = WinWorker(id=worker_id, name=name, token_hash=secret_hash(secret), enabled=True,
                    signs=signs, created_at=time.time())  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(row)
    return row, f"{TOKEN_PREFIX}_{worker_id}_{secret}"


async def worker_of(db: Database, authorization: str, version: str = "") -> WinWorker | None:
    """The enabled worker whose token the header carries, else None (and mark it seen)."""
    if not authorization.lower().startswith("bearer "):
        return None
    parts = authorization[7:].strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    async with db.session() as session, session.begin():
        row = await session.get(WinWorker, parts[1])
        if row is None or not hmac.compare_digest(row.token_hash, secret_hash(parts[2])):
            return None
        if not row.enabled:
            return None
        now = time.time()
        if now - row.last_seen > SEEN_EVERY_S or (version and version != row.version):
            row.last_seen = now
            row.version = version or row.version
    return row
