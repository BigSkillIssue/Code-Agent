"""Stripe's webhooks (W31), `POST /api/billing/stripe/webhook`.

An event counts only with Stripe's signature: an HMAC-SHA256 of "<timestamp>.<body>" with the
webhook secret, and a timestamp at most five minutes from now (so an old delivery cannot be
replayed). The event is stored and answered at once; handling it follows in the background.
"""

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from forge_web.billing.events import Events
from forge_web.services import services_of

TOLERANCE_S = 300.0
MAX_BODY = 512 * 1024


def signature_ok(payload: bytes, header: str, secret: str, now: float) -> bool:
    """Whether `Stripe-Signature` signs this payload with the secret, and recently."""
    parts = [part.partition("=") for part in header.split(",")]
    stamps = [value for name, _, value in parts if name == "t"]
    signatures = [value for name, _, value in parts if name == "v1"]
    if len(stamps) != 1 or not stamps[0].isdigit() or not signatures:
        return False
    if abs(now - int(stamps[0])) > TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), stamps[0].encode() + b"." + payload, hashlib.sha256)
    return any(hmac.compare_digest(expected.hexdigest(), given) for given in signatures)


def stripe_event(payload: bytes) -> dict[str, Any]:
    """The event in a signed payload; ValueError when it is not one."""
    event = json.loads(payload)
    if not isinstance(event, dict) or event.get("object") != "event":
        raise ValueError("not a Stripe event")
    if not str(event.get("id", "")).startswith("evt_") or not event.get("type"):
        raise ValueError("an event without its id or type")
    float(event["created"])
    return event


def webhook_router() -> APIRouter:
    """Stripe's deliveries (no session: the signature is the proof)."""
    router = APIRouter(prefix="/api/billing/stripe")

    @router.post("/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        services = services_of(request)
        keys = await services.billing.keys()
        if keys is None or not services.settings.billing.enabled:
            raise HTTPException(503, "billing is not set up")
        payload = await request.body()
        if len(payload) > MAX_BODY:
            raise HTTPException(413, "too large for a Stripe event")
        header = request.headers.get("stripe-signature", "")
        if not signature_ok(payload, header, keys.webhook_secret, time.time()):
            raise HTTPException(400, "the signature does not fit")
        try:
            event = stripe_event(payload)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "not a Stripe event") from None
        if bool(event.get("livemode")) != (keys.mode == "live"):
            raise HTTPException(400, "an event of the other mode (test or live)")
        if await services.billing.events.store(event):
            services.background(handled(services.billing.events))
        return {"received": True}

    return router


async def handled(events: Events) -> None:
    """Handle what waits (in the background, after Stripe has its answer)."""
    await events.handle_pending()
