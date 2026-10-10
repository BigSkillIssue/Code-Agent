"""Apple's App Store Connect API, with a user's team key (W22).

Every request carries a short-lived JWT signed with the key (ES256, as Apple requires); the key
never leaves the server. Apple's error messages come back as `AscError` with Apple's own words.
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

AUDIENCE = "appstoreconnect-v1"
TOKEN_S = 15 * 60  # Apple accepts at most 20 minutes
RETRIES = 3  # after "too many requests"
MAX_WAIT_S = 30.0


class AscError(Exception):
    """App Store Connect refused a request (or could not be reached)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AscKey:
    """A team key of App Store Connect: its ids and the private key (PEM)."""

    key_id: str
    issuer_id: str
    team_id: str
    private_pem: str


def check_private_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """The key from an `AuthKey_….p8` file; ValueError when it is not an Apple API key."""
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as err:
        raise ValueError("this is not a private key in PEM form (the .p8 file)") from err
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("App Store Connect keys are P-256 EC keys; this one is not")
    return key


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(key: AscKey, now: float) -> str:
    """A JWT for App Store Connect, valid for TOKEN_S seconds from `now`."""
    header = {"alg": "ES256", "kid": key.key_id, "typ": "JWT"}
    payload = {"iss": key.issuer_id, "iat": int(now), "exp": int(now) + TOKEN_S, "aud": AUDIENCE}
    signing = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(payload).encode())
    der = check_private_key(key.private_pem).sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)  # JWT wants r and s side by side, not DER
    return signing + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


@dataclass
class AscClient:
    """Calls App Store Connect as the owner of one key."""

    base_url: str
    key: AscKey
    http: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
    )
    _token: tuple[str, float] = ("", 0.0)

    def bearer(self) -> str:
        """The current token, renewed a minute before it runs out."""
        now = time.time()
        if now > self._token[1] - 60:
            self._token = (token(self.key, now), now + TOKEN_S)
        return self._token[0]

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """One API call; the JSON answer (empty for no content)."""
        url = path if path.startswith("http") else self.base_url.rstrip("/") + path
        for attempt in range(RETRIES + 1):
            headers = {"Authorization": f"Bearer {self.bearer()}", "Accept": "application/json"}
            try:
                reply = await self.http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as err:
                raise AscError(0, f"App Store Connect is not reachable: {err}") from None
            if reply.status_code == 429 and attempt < RETRIES:
                await asyncio.sleep(min(float(reply.headers.get("Retry-After", 5)), MAX_WAIT_S))
                continue
            if reply.status_code >= 400:
                raise AscError(reply.status_code, apple_message(reply))
            return reply.json() if reply.content else {}
        raise AscError(429, "App Store Connect asks to slow down; try again in a minute")

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", path, json=body)

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", path, json=body)

    async def all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every item of a list, page after page."""
        page = await self.get(path, **params)
        items = list(page.get("data", []))
        while (following := page.get("links", {}).get("next")) and len(items) < 2000:
            page = await self.request("GET", following)
            items += page.get("data", [])
        return items

    async def check(self) -> str:
        """What the key may see, in one sentence; AscError when it does not work."""
        apps = await self.get("/v1/apps", limit=200)
        await self.get("/v1/bundleIds", limit=1)  # needs a team key with access to identifiers
        count = len(apps.get("data", []))
        return f"The key works: {count} app{'s' if count != 1 else ''} in App Store Connect."

    async def close(self) -> None:
        await self.http.aclose()


def apple_message(reply: httpx.Response) -> str:
    """Apple's own explanation of an error, or the status line."""
    try:
        errors = reply.json().get("errors", [])
    except ValueError:
        errors = []
    texts = [str(e.get("detail") or e.get("title") or "") for e in errors if isinstance(e, dict)]
    found = "; ".join(t for t in texts if t)[:1000]
    return found or f"App Store Connect answered {reply.status_code}"
