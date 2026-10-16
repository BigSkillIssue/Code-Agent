"""The payments relay's tokens (W35): one per hosted app and environment, made at its first deploy
that asks for payments, kept encrypted and sealed into every release as `PAYMENTS_RELAY_TOKEN`,
next to `PAYMENTS_RELAY_URL`. Apart from the relay's routes (`billing/relay.py`), so the deploys
can make them without importing the web routes.
"""

import hmac
import secrets
import time

from forge_web.db.billing_models import AppPayments
from forge_web.db.engine import Database
from forge_web.settings import WebSettings
from forge_web.vault import Vault

TOKEN_PREFIX = "fpr"
TOKEN_SECRET = "PAYMENTS_RELAY_TOKEN"
URL_SETTING = "PAYMENTS_RELAY_URL"


def payments_url(settings: WebSettings) -> str:
    """Where releases ask for payments."""
    return f"{settings.base_url()}/api/relay/payments"


async def payments_token(db: Database, vault: Vault, project_id: str, environment: str) -> str:
    """The app's payments token for this environment: made once, then kept."""
    async with db.session() as session, session.begin():
        row = await session.get(AppPayments, (project_id, environment))
        if row is None:
            token = f"{TOKEN_PREFIX}_{project_id}_{environment}_{secrets.token_urlsafe(32)}"
            encrypted, now = vault.encrypt(token), time.time()
            session.add(AppPayments(project_id=project_id, environment=environment,
                                    token=encrypted, created_at=now))  # fmt: skip
            return token
        return vault.decrypt(row.token)


async def payments_of(db: Database, vault: Vault, authorization: str) -> AppPayments | None:
    """The app whose token the header carries, else None."""
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    parts = token.split("_", 3)
    if len(parts) != 4 or parts[0] != TOKEN_PREFIX:
        return None
    async with db.session() as session:
        row = await session.get(AppPayments, (parts[1], parts[2]))
    if row is None or not hmac.compare_digest(vault.decrypt(row.token), token):
        return None
    return row
