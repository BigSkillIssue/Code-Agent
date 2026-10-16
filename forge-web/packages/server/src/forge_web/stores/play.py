"""Google Play's Developer API (androidpublisher v3), with a user's service account (W37b).

The service account's key signs a JWT (RS256) that Google's OAuth server trades for an access
token of an hour; the key never leaves this server. A release is one edit: open it, upload the
signed bundle, put its version code on a track, commit. Google's error messages come back as
`PlayError` in Google's own words.
"""

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_S = 3600  # Google's longest
PACKAGE = re.compile(r"^[A-Za-z]\w*(\.[A-Za-z]\w*)+$")
Track = Literal["internal", "alpha", "beta", "production"]  # alpha: the closed test


class PlayError(Exception):
    """Google Play refused a request (or could not be reached)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ServiceAccount(BaseModel):
    """What Forge Web keeps of a service account's JSON key file."""

    model_config = ConfigDict(extra="ignore")
    type: Literal["service_account"]
    client_email: str = Field(
        pattern=r"^[^@\s]{1,200}@[a-z0-9-]{1,100}\.iam\.gserviceaccount\.com$"
    )
    private_key_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    private_key: str = Field(min_length=100, max_length=8_000)


def check_private_key(pem: str) -> rsa.RSAPrivateKey:
    """The service account's key; ValueError when it is not an RSA key in PEM form."""
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as err:
        raise ValueError("the key file's private_key is not a private key in PEM form") from err
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("a service account's key is an RSA key; this one is not")
    return key


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def assertion(account: ServiceAccount, audience: str, now: float) -> str:
    """The signed JWT Google's OAuth server takes for an access token."""
    header = {"alg": "RS256", "typ": "JWT", "kid": account.private_key_id}
    claims = {"iss": account.client_email, "scope": SCOPE, "aud": audience,
              "iat": int(now), "exp": int(now) + TOKEN_S}  # fmt: skip
    signing = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(claims).encode())
    key = check_private_key(account.private_key)
    signature = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + b64url(signature)


async def chunks(path: Path, size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """A file in parts, read off the event loop (a bundle can be large)."""
    with path.open("rb") as handle:
        while part := await asyncio.to_thread(handle.read, size):
            yield part


def google_message(reply: httpx.Response) -> str:
    """Google's own explanation of an error, or the status line."""
    try:
        found = reply.json()
    except ValueError:
        found = {}
    error = found.get("error") if isinstance(found, dict) else None
    if isinstance(error, dict):
        text = str(error.get("message") or "")
    else:  # the OAuth server answers {"error": "...", "error_description": "..."}
        text = str(found.get("error_description") or error or "") if isinstance(found, dict) else ""
    return text[:1000] or f"Google Play answered {reply.status_code}"


@dataclass
class PlayClient:
    """Calls the Play Developer API as one service account."""

    api_url: str
    token_url: str
    account: ServiceAccount
    http: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20))
    )
    _token: tuple[str, float] = ("", 0.0)

    async def bearer(self) -> str:
        """The current access token, renewed five minutes before it runs out."""
        now = time.time()
        if now > self._token[1] - 300:
            form = {"grant_type": GRANT, "assertion": assertion(self.account, self.token_url, now)}
            answer = await self.call("POST", self.token_url, data=form, signed=False)
            token = str(answer.get("access_token", ""))
            if not token:
                raise PlayError(0, "Google gave no access token")
            self._token = (token, now + float(answer.get("expires_in", TOKEN_S)))
        return self._token[0]

    async def call(self, method: str, url: str, signed: bool = True, **kwargs: Any) -> Any:
        """One request; the JSON answer (empty for no content)."""
        headers = {"Accept": "application/json"} | kwargs.pop("headers", {})
        if signed:
            headers["Authorization"] = f"Bearer {await self.bearer()}"
        try:
            reply = await self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as err:
            raise PlayError(0, f"Google Play is not reachable: {err}") from None
        if reply.status_code >= 400:
            raise PlayError(reply.status_code, google_message(reply))
        return reply.json() if reply.content else {}

    def app_url(self, package: str, upload: bool = False) -> str:
        if not PACKAGE.match(package):
            raise PlayError(0, f"{package!r} is not an Android application ID")
        prefix = "/upload" if upload else ""
        return f"{self.api_url.rstrip('/')}{prefix}/androidpublisher/v3/applications/{package}"

    async def release(
        self,
        package: str,
        bundle: Path,
        track: str,
        version_code: int,
        name: str,
        notes: str,
        status: Literal["completed", "draft"] = "completed",
    ) -> int:
        """Upload the bundle and release it on the track in one edit; its version code."""
        edit = await self.call("POST", f"{self.app_url(package)}/edits", json={})
        edit_id = str(edit["id"])
        uploaded = await self.call(
            "POST", f"{self.app_url(package, upload=True)}/edits/{edit_id}/bundles",
            params={"uploadType": "media"}, content=chunks(bundle),
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(bundle.stat().st_size)},
        )  # fmt: skip
        if int(uploaded.get("versionCode", 0)) != version_code:
            raise PlayError(0, f"Google Play read version code {uploaded.get('versionCode')} "
                               f"from the bundle, not {version_code}")  # fmt: skip
        entry: dict[str, Any] = {"name": name, "versionCodes": [str(version_code)],
                                 "status": status}  # fmt: skip
        if notes:
            entry["releaseNotes"] = [{"language": "en-US", "text": notes[:500]}]
        await self.call("PUT", f"{self.app_url(package)}/edits/{edit_id}/tracks/{track}",
                        json={"track": track, "releases": [entry]})  # fmt: skip
        await self.call("POST", f"{self.app_url(package)}/edits/{edit_id}:commit")
        return version_code

    async def check(self, package: str) -> str:
        """Whether the account may publish this app, in one sentence (PlayError when not)."""
        edit = await self.call("POST", f"{self.app_url(package)}/edits", json={})
        await self.call("DELETE", f"{self.app_url(package)}/edits/{edit['id']}")
        return f"The service account may publish {package} on Google Play."

    async def close(self) -> None:
        await self.http.aclose()
