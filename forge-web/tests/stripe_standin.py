"""Stripe for the billing tests (W31): its API as an httpx transport that keeps the objects it
made and answers each idempotency key once (a repeated key gets the first answer again, as Stripe
does), can lose answers or fail on purpose; and Stripe's way of signing webhook events."""

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


class StripeStandIn:
    """Stripe's API, as far as the tests need it."""

    def __init__(self, secret_key: str = TEST_KEY) -> None:
        self.secret_key = secret_key
        self.requests: list[httpx.Request] = []
        self.effects: list[tuple[str, str, dict[str, str]]] = []  # what was done, once each
        self.answered: dict[str, httpx.Response] = {}  # idempotency key -> its first answer
        self.trouble: list[str | int] = []  # for the next calls: "down", "lost" or a status
        self.ids = itertools.count(1)

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
        if request.method == "POST" and path == "/v1/customers":
            self.effects.append(("POST", path, params))
            customer = {"id": f"cus_{next(self.ids)}", "object": "customer",
                        "email": params.get("email", "")}  # fmt: skip
            return httpx.Response(200, json=customer)
        if request.method == "POST" and path == "/v1/payment_intents":
            return error(402, "Your card was declined.", "card_declined")
        if request.method == "GET" and path == "/v1/balance":
            return httpx.Response(200, json={"object": "balance", "livemode": False})
        return error(404, f"Unrecognized request URL ({request.method}: {path}).")


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
