"""The audit log: who did what, from where."""

import json
import time
from typing import Any

from forge_web.db.engine import Database
from forge_web.db.models import AuditEntry


async def audit(
    db: Database,
    action: str,
    *,
    user_id: str = "",
    target: str = "",
    ip: str = "",
    **detail: Any,
) -> None:
    """Record one entry."""
    entry = AuditEntry(
        created_at=time.time(), user_id=user_id, action=action, target=target[:200],
        detail=json.dumps(detail, default=str)[:4000] if detail else "", ip=ip[:64],
    )  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(entry)
