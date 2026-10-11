"""Stripe for the billing tests (W31, W32): its API as an httpx transport that keeps the objects
it made (customers, products, prices, coupons, promotion codes) and answers each idempotency key
once (a repeated key gets the first answer again, as Stripe does), can lose answers or fail on
purpose, and refuses what Stripe refuses (a price's amount never changes); and Stripe's way of
signing webhook events."""

import hashlib
import hmac
import itertools
import json
import time
from typing import Any
from urllib.parse import parse_qsl

import httpx

# Made of parts, so no scanner takes these stand-in values for real keys.
TEST_KEY = "_".join(("sk", "test", "standin0standin0standin0"))
LIVE_KEY = "_".join(("sk", "live", "standin0standin0standin0"))
WEBHOOK_SECRET = "_".join(("whsec", "standin0standin0standin0standin0"))
MADE = {"/v1/customers": "cus", "/v1/products": "prod", "/v1/prices": "price",
        "/v1/coupons": "coupon", "/v1/promotion_codes": "promo",
        "/v1/checkout/sessions": "cs", "/v1/subscriptions": "sub",
        "/v1/billing_portal/sessions": "bps"}  # fmt: skip


class StripeStandIn:
    """Stripe's API, as far as the tests need it."""

    def __init__(self, secret_key: str = TEST_KEY) -> None:
        self.secret_key = secret_key
        self.requests: list[httpx.Request] = []
        self.effects: list[tuple[str, str, dict[str, str]]] = []  # what was done, once each
        self.answered: dict[str, httpx.Response] = {}  # idempotency key -> its first answer
        self.trouble: list[str | int] = []  # for the next calls: "down", "lost" or a status
        self.ids = itertools.count(1)
        self.objects: dict[str, dict[str, Any]] = {}  # id -> what Stripe keeps

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        trouble = self.trouble.pop(0) if self.trouble else None
        if trouble == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.secret_key}":
            return error(401, "Invalid API Key provided: sk_test_****" + self.secret_key[-4:])
        if isinstance(trouble, int):
            return error(trouble, "Something went wrong on Stripe's end.")
        key = request.headers.get("idempotency-key", "")
        if key in self.answered:
            first = self.answered[key]
            return httpx.Response(first.status_code, content=first.content,
                                  headers={"Idempotent-Replayed": "true"})  # fmt: skip
        answer = self.handle(request)
        if key:
            self.answered[key] = answer
        if trouble == "lost":  # done, but the answer never arrives
            raise httpx.ReadTimeout("timed out", request=request)
        return answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode())) if request.content else {}
        path = request.url.path
        if request.method == "DELETE":
            self.effects.append(("DELETE", path, {}))
            return httpx.Response(200, json={"deleted": True})
        if request.method == "POST" and path in MADE:
            return self.make(path, params)
        folder, _, object_id = path.rpartition("/")
        if request.method == "POST" and folder in MADE and object_id in self.objects:
            return self.change(path, folder, object_id, params)
        if request.method == "POST" and path == "/v1/payment_intents":
            return error(402, "Your card was declined.", "card_declined")
        if request.method == "GET" and path == "/v1/balance":
            return httpx.Response(200, json={"object": "balance", "livemode": False})
        return error(404, f"Unrecognized request URL ({request.method}: {path}).")

    def make(self, path: str, params: dict[str, str]) -> httpx.Response:
        object_id = params.get("id") or f"{MADE[path]}_{next(self.ids)}"
        if object_id in self.objects:
            return error(400, f"{object_id} already exists", "resource_already_exists")
        made = {"object": MADE[path], **params, "id": object_id,
                "active": params.get("active", "true") != "false"}  # fmt: skip
        if path == "/v1/checkout/sessions":  # where Stripe's own page takes the card
            made["url"] = f"https://checkout.stripe.com/c/pay/{object_id}"
        if path == "/v1/billing_portal/sessions":
            made["url"] = f"https://billing.stripe.com/p/session/{object_id}"
        self.objects[object_id] = made
        self.effects.append(("POST", path, params))
        return httpx.Response(200, json=made)

    def change(
        self, path: str, folder: str, object_id: str, params: dict[str, str]
    ) -> httpx.Response:
        if folder == "/v1/prices" and {"unit_amount", "currency", "recurring[interval]"} & set(
            params
        ):
            return error(400, "A price's amount cannot be changed; make a new price.")
        changed = self.objects[object_id]
        changed.update(params)
        changed["active"] = (
            params.get("active", "true" if changed["active"] else "false") != "false"
        )
        self.effects.append(("POST", path, params))
        return httpx.Response(200, json=changed)

    def of(self, kind: str) -> list[dict[str, Any]]:
        """The objects of one kind ("prod", "price", ...), in the order they were made."""
        return [o for o in self.objects.values() if o["object"] == kind]


def error(status: int, message: str, code: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "code": code,
                                                  "type": "invalid_request_error"}})  # fmt: skip


def event(event_id: str, kind: str, created: float, obj: dict[str, Any]) -> dict[str, Any]:
    """A Stripe event as webhooks deliver it."""
    return {"id": event_id, "object": "event", "type": kind, "created": int(created),
            "livemode": False, "data": {"object": obj}}  # fmt: skip


def signed(payload: bytes, secret: str = WEBHOOK_SECRET, at: float | None = None) -> str:
    """The `Stripe-Signature` header Stripe sends with a payload."""
    stamp = int(time.time() if at is None else at)
    mac = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256)
    return f"t={stamp},v1={mac.hexdigest()}"


def body(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()
