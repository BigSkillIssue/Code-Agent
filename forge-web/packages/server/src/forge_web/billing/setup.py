"""The admin's Stripe keys (W31): the secret key and the webhook signing secret, kept encrypted
with the vault and never shown again (only their mode and last characters). Test keys first: a
live key needs `billing.allow_live`.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from forge_web.billing.catalog import Catalog
from forge_web.billing.events import Events
from forge_web.billing.stripe_client import StripeClient, key_mode
from forge_web.db.billing_models import BillingKeys
from forge_web.db.engine import Database
from forge_web.settings import WebSettings
from forge_web.vault import Vault

KEYS_ROW = 1
WEBHOOK_PATH = "/api/billing/stripe/webhook"


class StripeKeysIn(BaseModel):
    """The keys from Stripe's dashboard (Developers → API keys, and the webhook's secret)."""

    model_config = ConfigDict(extra="forbid")
    secret_key: str = Field(min_length=20, max_length=300)
    webhook_secret: str = Field(min_length=10, max_length=300)


@dataclass(frozen=True)
class StripeKeys:
    """The keys in plain text, for this server's own use only."""

    secret_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    mode: str


class Billing:
    """Billing's parts: the keys, Stripe's client, its events and the catalog."""

    def __init__(self, db: Database, vault: Vault, settings: WebSettings) -> None:
        self.db, self.vault, self.settings = db, vault, settings
        self.events = Events(db)
        self.catalog = Catalog(db)
        self.transport: httpx.AsyncBaseTransport | None = None  # tests: Stripe's stand-in

    async def keys(self) -> StripeKeys | None:
        """The admin's keys, if set."""
        async with self.db.session() as session:
            row = await session.get(BillingKeys, KEYS_ROW)
        if row is None:
            return None
        vault = self.vault
        return StripeKeys(
            vault.decrypt(row.secret_key), vault.decrypt(row.webhook_secret), row.mode
        )

    async def client(self) -> StripeClient | None:
        """Stripe's client with the admin's key (None until the keys are set)."""
        keys = await self.keys()
        return StripeClient(keys.secret_key, transport=self.transport) if keys else None

    async def save(self, body: StripeKeysIn, user_id: str) -> None:
        """Keep new keys (ValueError for a key that is not one, PermissionError for live keys
        while live payments are not allowed)."""
        mode = key_mode(body.secret_key.strip())
        if not body.webhook_secret.strip().startswith("whsec_"):
            raise ValueError("the webhook's signing secret starts with whsec_")
        if mode == "live" and not self.settings.billing.allow_live:
            raise PermissionError("live keys need billing.allow_live (test mode first)")
        secret, hook = body.secret_key.strip(), body.webhook_secret.strip()
        async with self.db.session() as session, session.begin():
            row = await session.get(BillingKeys, KEYS_ROW)
            if row is None:
                row = BillingKeys(id=KEYS_ROW, secret_key="", webhook_secret="", mode=mode,
                                  hint="", updated_at=0.0)  # fmt: skip
                session.add(row)
            row.secret_key, row.webhook_secret = (
                self.vault.encrypt(secret),
                self.vault.encrypt(hook),
            )
            row.mode, row.hint = mode, secret[-4:]
            row.updated_at, row.updated_by = time.time(), user_id

    async def view(self) -> dict[str, Any]:
        """What the admin sees of the keys: whether they are set, the mode, the last characters."""
        async with self.db.session() as session:
            row = await session.get(BillingKeys, KEYS_ROW)
        return {"configured": row is not None, "mode": row.mode if row else "",
                "hint": row.hint if row else "", "updated_at": row.updated_at if row else 0.0,
                "enabled": self.settings.billing.enabled,
                "webhook_url": self.settings.base_url() + WEBHOOK_PATH}  # fmt: skip
