"""Stripe's API (W31), with the admin's secret key from the vault.

Stripe takes form-encoded bodies (`metadata[app]=shop`, `line_items[0][price]=...`) and answers
JSON. Every changing call carries an idempotency key, kept across retries: a call that timed out
or got a 5xx or 429 is sent again with the same key, so Stripe does it at most once. The key is
never logged and never part of an error; Stripe's own message comes back as `StripeError`.
Test mode first: a live key (`sk_live_...`) is refused unless the admin allowed live payments.
"""

import asyncio
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "https://api.stripe.com"
VERSION = "2025-09-30.clover"  # the API version Forge Web is written for
RETRIES = 3
WAIT_S = (0.5, 1.0, 2.0)  # between tries; Stripe's Retry-After wins when it is shorter
SAFE = ("GET", "DELETE")  # repeatable without an idempotency key


class StripeError(Exception):
    """Stripe refused a call (or could not be reached); its own message, never the key."""

    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status, self.code = status, code


def key_mode(secret_key: str) -> str:
    """ "test" or "live" for a Stripe secret or restricted key; ValueError for anything else."""
    for prefix in ("sk_", "rk_"):
        for mode in ("test", "live"):
            if secret_key.startswith(f"{prefix}{mode}_") and len(secret_key) > 20:
                return mode
    raise ValueError("this is not a Stripe secret key (sk_test_... or sk_live_...)")


def form(params: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    """Stripe's form encoding of nested parameters."""
    for name, value in params.items():
        key = f"{prefix}[{name}]" if prefix else str(name)
        if isinstance(value, Mapping):
            yield from form(value, key)
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    yield from form(item, f"{key}[{index}]")
                else:
                    yield f"{key}[{index}]", plain(item)
        elif value is not None:
            yield key, plain(value)


def plain(value: Any) -> str:
    """One form value as Stripe reads it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)  # 25.0 -> "25"
    return str(value)


@dataclass
class StripeClient:
    """Calls Stripe with one secret key."""

    secret_key: str = field(repr=False)
    base_url: str = API
    transport: httpx.AsyncBaseTransport | None = None
    http: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(40, connect=10),
                                      transport=self.transport)  # fmt: skip

    async def call(self, method: str, path: str, params: Mapping[str, Any] | None = None,
                   idempotency_key: str = "") -> dict[str, Any]:  # fmt: skip
        """One API call, tried again on network trouble, 429 and 5xx with the same key."""
        headers = {"Authorization": f"Bearer {self.secret_key}", "Stripe-Version": VERSION}
        if method not in SAFE:
            headers["Idempotency-Key"] = idempotency_key or secrets.token_hex(16)
        pairs = list(form(params or {}))  # names are unique, so a dict keeps them all
        query = httpx.QueryParams(tuple(pairs)) if method in SAFE else None
        data = None if method in SAFE else dict(pairs)
        for attempt in range(RETRIES + 1):
            try:
                reply = await self.http.request(method, path, headers=headers, params=query,
                                                data=data)  # fmt: skip
            except httpx.TransportError as error:
                if attempt < RETRIES:
                    await asyncio.sleep(WAIT_S[min(attempt, len(WAIT_S) - 1)])
                    continue
                raise StripeError(0, f"Stripe is not reachable: {type(error).__name__}") from None
            if (reply.status_code == 429 or reply.status_code >= 500) and attempt < RETRIES:
                await asyncio.sleep(wait_for(reply, attempt))
                continue
            if reply.status_code >= 400:
                raise refusal(reply, self.secret_key)
            return dict(reply.json())
        raise StripeError(429, "Stripe asks to slow down; try again in a minute")

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.call("GET", path, params)

    async def post(
        self, path: str, params: Mapping[str, Any], idempotency_key: str = ""
    ) -> dict[str, Any]:
        return await self.call("POST", path, params, idempotency_key)

    async def close(self) -> None:
        await self.http.aclose()


def wait_for(reply: httpx.Response, attempt: int) -> float:
    """How long to wait before trying again."""
    ours = WAIT_S[min(attempt, len(WAIT_S) - 1)]
    try:
        return min(float(reply.headers.get("Retry-After", ours)), ours * 4)
    except ValueError:
        return ours


def refusal(reply: httpx.Response, secret_key: str) -> StripeError:
    """Stripe's own explanation of an error (with the key cut out, should Stripe repeat it)."""
    try:
        error = reply.json().get("error", {})
    except ValueError:
        error = {}
    message = str(error.get("message") or f"Stripe answered {reply.status_code}")[:1000]
    message = message.replace(secret_key, "***").replace(secret_key[8:], "***")
    return StripeError(
        reply.status_code, message, str(error.get("code") or error.get("type") or "")
    )
