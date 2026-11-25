"""Sign in with Apple and Facebook Login, against stand-ins for Apple and Meta."""

import base64
import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from forge_web.auth.oauth_providers import load_providers
from forge_web.auth.oauth_social import apple_name
from forge_web.settings import ProviderSettings, load_settings
from support import LiveServer, WebClient
from test_oauth import error_of, jwt

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
APPLE = {"client_id": "com.example.forge.web", "client_secret": PEM, "team_id": "TEAM123456",
         "key_id": "KEY1234567"}  # fmt: skip
FACEBOOK = {"client_id": "fb-app", "client_secret": "fb-secret"}


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def apple_secret_is_good(secret: str) -> bool:
    """Apple's check of the client secret: signed by our key, about our app, fresh."""
    head, body, signature = secret.split(".")
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    KEY.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    header, claims = json.loads(unb64(head)), json.loads(unb64(body))
    return (header == {"alg": "ES256", "kid": "KEY1234567", "typ": "JWT"}
            and claims["iss"] == "TEAM123456" and claims["sub"] == APPLE["client_id"]
            and claims["aud"] == "https://appleid.apple.com"
            and claims["iat"] <= time.time() < claims["exp"])  # fmt: skip


class Stand:
    """Apple's and Meta's endpoints, as far as the server talks to them."""

    def __init__(self) -> None:
        self.asked: dict[str, dict[str, str]] = {}
        self.apple: dict[str, Any] = {"sub": "000123.abc", "email": "ada@example.com",
                                      "email_verified": "true"}  # fmt: skip
        self.facebook: dict[str, Any] = {"id": "10001", "name": "Ada Lovelace",
                                         "email": "ada@example.com"}  # fmt: skip

    def authorize(self, url: str) -> dict[str, str]:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        query["code"] = secrets.token_hex(8)
        self.asked[query["code"]] = query
        return query

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        asked = self.asked.pop(form.get("code", ""), None) if request.method == "POST" else None
        if url == "https://appleid.apple.com/auth/token":
            if (
                asked is None
                or "code_verifier" in form
                or not apple_secret_is_good(form["client_secret"])
            ):
                return httpx.Response(400, json={"error": "invalid_client"})
            claims = {"iss": "https://appleid.apple.com", "aud": asked["client_id"],
                      "exp": time.time() + 600, "nonce": asked["nonce"], **self.apple}  # fmt: skip
            return httpx.Response(200, json={"access_token": "a", "id_token": jwt(claims)})
        if url == "https://graph.facebook.com/v23.0/oauth/access_token":
            if asked is None or form.get("client_secret") != "fb-secret" or "code_verifier" in form:
                return httpx.Response(400, json={"error": {"message": "bad"}})
            return httpx.Response(200, json={"access_token": "fb_access"})
        if url == "https://graph.facebook.com/v23.0/me":
            if request.headers.get("authorization") != "Bearer fb_access":
                return httpx.Response(401, json={"error": {"message": "bad token"}})
            return httpx.Response(200, json=self.facebook)
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def stand() -> Stand:
    return Stand()


def social_server(data_dir: Path, stand: Stand, **facebook: Any) -> Iterator[LiveServer]:
    providers = {"apple": APPLE, "facebook": {**FACEBOOK, **facebook}}
    settings = load_settings(
        data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides={"sandbox.isolation": "docker", "auth.signup": "open",
                   "auth.providers": providers},
    )  # fmt: skip
    with LiveServer(settings) as live:
        live.services.sign_in.http = httpx.AsyncClient(transport=httpx.MockTransport(stand.handler))
        yield live


@pytest.fixture
def server(tmp_path: Path, stand: Stand) -> Iterator[LiveServer]:
    yield from social_server(tmp_path / "data", stand)


async def apple_sign_in(web: WebClient, stand: Stand, origin: str = "https://appleid.apple.com",
                        user: str = "") -> httpx.Response:  # fmt: skip
    """Click "Sign in with Apple", agree at Apple, and let Apple post the answer back."""
    start = await web.client.get("/api/auth/oauth/apple/start")
    query = stand.authorize(start.headers["location"])
    posted = await web.client.post(
        "/api/auth/oauth/apple/callback", headers={"origin": origin},
        data={"code": query["code"], "state": query["state"], "user": user},
    )  # fmt: skip
    if posted.status_code != 303:
        return posted
    return await web.client.get(posted.headers["location"])


def test_apple_and_facebook_need_their_settings() -> None:
    configured = {
        "apple": ProviderSettings(client_id="x", client_secret=PEM),  # no team or key id
        "facebook": ProviderSettings(client_id="fb", client_secret="s"),
        "apple-2": ProviderSettings(
            kind="apple", client_id="x", client_secret="not a key", team_id="T", key_id="K"
        ),
    }
    found = load_providers(configured, environ={})
    assert list(found) == ["facebook"] and found["facebook"].label == "Facebook"
    assert apple_name('{"name": {"firstName": "Ada", "lastName": "Lovelace"}}') == "Ada Lovelace"
    assert apple_name("not json") == apple_name("") == ""


async def test_sign_in_with_apple(server: LiveServer, stand: Stand) -> None:
    async with WebClient(server) as web:
        providers = (await web.get("/api/auth/config")).json()["providers"]
        assert [p["kind"] for p in providers] == ["apple", "facebook"]
        start = await web.client.get("/api/auth/oauth/apple/start")
        location = start.headers["location"]
        assert location.startswith("https://appleid.apple.com/auth/authorize?")
        asked = parse_qs(urlsplit(location).query)
        assert asked["response_mode"] == ["form_post"] and asked["scope"] == ["name email"]
        assert "code_challenge" not in asked and asked["nonce"]
        user = json.dumps({"name": {"firstName": "Ada", "lastName": "Lovelace"}})
        back = await apple_sign_in(web, stand, user=user)
        assert back.status_code == 302 and error_of(back) == "", back.headers
        me = (await web.get("/api/me")).json()
        assert me["email"] == "ada@example.com" and me["name"] == "Ada Lovelace"


async def test_apple_answers_only_from_apple_and_only_once(
    server: LiveServer, stand: Stand
) -> None:
    async with WebClient(server) as web:
        refused = await apple_sign_in(web, stand, origin="https://evil.example")
        assert refused.status_code == 403  # the origin guard
        start = await web.client.get("/api/auth/oauth/apple/start")
        query = stand.authorize(start.headers["location"])
        forged = await web.client.post(
            "/api/auth/oauth/apple/callback", headers={"origin": "https://appleid.apple.com"},
            data={"code": query["code"], "state": "not-the-state"},
        )  # fmt: skip
        back = await web.client.get(forged.headers["location"])
        assert "not valid" in error_of(back)
        assert (await web.client.get("/api/me")).status_code == 401
    async with WebClient(server) as web, WebClient(server) as stranger:
        start = await web.client.get("/api/auth/oauth/apple/start")
        query = stand.authorize(start.headers["location"])
        posted = await stranger.client.post(  # an answer that arrives in another browser
            "/api/auth/oauth/apple/callback", headers={"origin": "https://appleid.apple.com"},
            data={"code": query["code"], "state": query["state"]},
        )  # fmt: skip
        back = await stranger.client.get(posted.headers["location"])
        assert "not valid" in error_of(back)
        assert (await stranger.client.get("/api/me")).status_code == 401


async def test_facebook_needs_trusted_emails(tmp_path: Path, stand: Stand) -> None:
    for trusted, data in ((False, "plain"), (True, "trusted")):
        for live in social_server(tmp_path / data, stand, trust_email=trusted):
            async with WebClient(live) as web:
                start = await web.client.get("/api/auth/oauth/facebook/start")
                location = start.headers["location"]
                assert location.startswith("https://www.facebook.com/v23.0/dialog/oauth?")
                asked = parse_qs(urlsplit(location).query)
                assert asked["scope"] == ["email,public_profile"] and "code_challenge" not in asked
                query = stand.authorize(location)
                back = await web.client.get(
                    f"/api/auth/oauth/facebook/callback?code={query['code']}&state={query['state']}"
                )
                if trusted:
                    assert error_of(back) == ""
                    me = (await web.get("/api/me")).json()
                    assert me["email"] == "ada@example.com" and me["name"] == "Ada Lovelace"
                else:
                    assert "no verified email" in error_of(back)
