"""Signing in with Google, GitHub and OpenID Connect, against a fake identity provider."""

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from forge_web.auth.oauth_providers import (
    claims_problem,
    jwt_claims,
    load_providers,
    pkce_pair,
)
from forge_web.auth.totp import new_secret, secret_bytes, totp
from forge_web.db.models import AuditEntry, GitCredential, Identity, User
from forge_web.settings import ProviderSettings, load_settings
from support import LiveServer, WebClient, person

PROVIDERS = {
    "google": {"client_id": "google-app", "client_secret": "g-secret"},
    "github": {"client_id": "github-app", "client_secret": "gh-secret"},
    "firma": {"client_id": "firma-app", "client_secret": "f-secret",
              "issuer": "https://sso.example.com", "label": "Firmen-Login"},
}  # fmt: skip


def jwt(claims: dict[str, Any]) -> str:
    """An (unsigned) JWT carrying these claims."""
    parts = [{"alg": "RS256"}, claims]
    encoded = [base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=") for p in parts]
    return ".".join([*encoded, "c2lnbmF0dXJl"])


class FakeIdP:
    """Google, GitHub and an OpenID Connect issuer, as far as the server talks to them."""

    def __init__(self) -> None:
        self.google: dict[str, Any] = {"sub": "g-1", "email": "ada@example.com",
                                       "email_verified": True, "name": "Ada"}  # fmt: skip
        self.firma: dict[str, Any] = {"sub": "f-1", "email": "ada@example.com"}
        self.github_user: dict[str, Any] = {"id": 42, "login": "ada-gh", "name": "Ada"}
        self.github_emails = [
            {"email": "other@example.com", "primary": False, "verified": True},
            {"email": "ada@example.com", "primary": True, "verified": True},
        ]
        self.claims: dict[str, Any] = {}  # overrides for the next ID token
        self.asked: dict[str, dict[str, str]] = {}  # code -> the authorization request

    def authorize(self, url: str) -> str:
        """The browser at the provider: the user agrees; the path it is sent back to."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = secrets.token_hex(8)
        self.asked[code] = query
        back = urlsplit(query["redirect_uri"])
        return f"{back.path}?code={code}&state={query['state']}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer the server's calls."""
        url = str(request.url).split("?")[0]
        if url in ("https://oauth2.googleapis.com/token", "https://sso.example.com/token",
                   "https://github.com/login/oauth/access_token"):  # fmt: skip
            return self.token(url, {k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if url == "https://sso.example.com/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": "https://sso.example.com",
                "authorization_endpoint": "https://sso.example.com/auth",
                "token_endpoint": "https://sso.example.com/token",
            })  # fmt: skip
        if request.headers.get("authorization") == "Bearer gho_access":
            if url == "https://api.github.com/user":
                return httpx.Response(200, json=self.github_user)
            if url == "https://api.github.com/user/emails":
                return httpx.Response(200, json=self.github_emails)
        return httpx.Response(404, json={"message": "Not Found"})

    def token(self, url: str, form: dict[str, str]) -> httpx.Response:
        asked = self.asked.pop(form.get("code", ""), None)
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        if asked is None or asked["code_challenge"] != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if url.startswith("https://github.com"):
            return httpx.Response(200, json={"access_token": "gho_access", "scope": asked["scope"]})
        google = url.startswith("https://oauth2")
        claims = {
            "iss": "https://accounts.google.com" if google else "https://sso.example.com",
            "aud": asked["client_id"], "exp": time.time() + 3600, "nonce": asked["nonce"],
            **(self.google if google else self.firma), **self.claims,
        }  # fmt: skip
        return httpx.Response(200, json={"access_token": "at", "id_token": jwt(claims)})


def settings(data_dir: Path, signup: str = "open", **firma: Any) -> Any:
    providers = {**PROVIDERS, "firma": {**PROVIDERS["firma"], **firma}}
    overrides = {"sandbox.isolation": "docker", "auth.signup": signup, "auth.providers": providers}
    return load_settings(
        data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides=overrides,
    )  # fmt: skip


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@contextmanager
def live_server(web_settings: Any, idp: FakeIdP) -> Iterator[LiveServer]:
    """A server whose sign-in providers are the fake ones."""
    with LiveServer(web_settings) as live:
        live.services.sign_in.http = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
        yield live


@pytest.fixture
def server(tmp_path: Path, idp: FakeIdP) -> Iterator[LiveServer]:
    with live_server(settings(tmp_path / "data"), idp) as live:
        yield live


async def sign_in(web: WebClient, idp: FakeIdP, provider: str, **params: str) -> httpx.Response:
    """Click the provider's button, agree at the provider, come back."""
    start = await web.client.get(f"/api/auth/oauth/{provider}/start", params=params)
    assert start.status_code == 302, start.text
    return await web.client.get(idp.authorize(start.headers["location"]))


def error_of(response: httpx.Response) -> str:
    """The error a failed sign-in shows (empty if there is none)."""
    query = parse_qs(urlsplit(response.headers.get("location", "")).query)
    return query.get("auth_error", [""])[0]


async def accounts(server: LiveServer) -> list[User]:
    async with server.services.db.session() as session:
        return list(await session.scalars(select(User)))


async def identities(server: LiveServer) -> list[Identity]:
    async with server.services.db.session() as session:
        return list(await session.scalars(select(Identity)))


def test_pkce_and_claims_rules() -> None:
    verifier, challenge = pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    now, issuers = time.time(), ("https://accounts.google.com",)
    good = {"iss": issuers[0], "aud": "app", "exp": now + 60, "nonce": "n", "sub": "1"}
    assert claims_problem(good, issuers, "app", "n", now) is None
    assert claims_problem({**good, "iss": "https://evil"}, issuers, "app", "n", now)
    assert claims_problem({**good, "aud": "other"}, issuers, "app", "n", now)
    assert claims_problem({**good, "aud": ["app", "x"]}, issuers, "app", "n", now)  # no azp
    assert claims_problem({**good, "exp": now - 600}, issuers, "app", "n", now)
    assert claims_problem({**good, "nonce": "m"}, issuers, "app", "n", now)
    assert jwt_claims(jwt(good))["sub"] == "1"


def test_providers_without_a_secret_are_off() -> None:
    configured = {"google": ProviderSettings(client_id="g"), "firma": ProviderSettings(
        client_id="f", client_secret_env="FIRMA_SECRET")}  # fmt: skip
    assert load_providers(configured, environ={}) == {}
    found = load_providers(configured, environ={"FORGE_WEB_GOOGLE_SECRET": "s"})
    assert list(found) == ["google"] and found["google"].label == "Google"


async def test_the_sign_in_page_lists_the_providers(server: LiveServer) -> None:
    async with WebClient(server) as web:
        providers = (await web.get("/api/auth/config")).json()["providers"]
    assert providers == [{"name": "google", "label": "Google"},
                         {"name": "github", "label": "GitHub"},
                         {"name": "firma", "label": "Firmen-Login"}]  # fmt: skip


async def test_google_creates_an_account_and_finds_it_again(
    server: LiveServer, idp: FakeIdP
) -> None:
    async with WebClient(server) as web:
        start = await web.client.get("/api/auth/oauth/google/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        assert start.headers["location"].startswith("https://accounts.google.com/")
        assert query["code_challenge_method"] == ["S256"] and query["nonce"] and query["state"]
        assert query["redirect_uri"] == ["http://127.0.0.1:8420/api/auth/oauth/google/callback"]
        back = await web.client.get(idp.authorize(start.headers["location"]))
        assert back.status_code == 302 and back.headers["location"] == "/", error_of(back)
        me = (await web.get("/api/me")).json()
        assert me["email"] == "ada@example.com" and me["name"] == "Ada"
    async with WebClient(server) as again:
        await sign_in(again, idp, "google")
        assert (await again.get("/api/me")).json()["id"] == me["id"]
    assert len(await accounts(server)) == 1
    assert [(i.provider, i.subject) for i in await identities(server)] == [("google", "g-1")]
    async with server.services.db.session() as session:
        methods = list(await session.scalars(select(AuditEntry.detail)))
    assert any('"method": "google"' in m for m in methods)


async def test_a_verified_email_links_the_existing_account(
    server: LiveServer, idp: FakeIdP
) -> None:
    ada = await person(server, "ada", email_verified=True)
    async with WebClient(server) as web:
        await sign_in(web, idp, "github")
        assert (await web.get("/api/me")).json()["id"] == ada.id
    assert [(i.provider, i.username) for i in await identities(server)] == [("github", "ada-gh")]


async def test_unverified_emails_are_never_linked(server: LiveServer, idp: FakeIdP) -> None:
    await person(server, "ada", email_verified=True)
    idp.google["email_verified"] = False
    idp.github_emails[1]["verified"] = False
    async with WebClient(server) as web:
        for provider in ("google", "github", "firma"):  # firma sends no email_verified
            back = await sign_in(web, idp, provider)
            assert "verified email" in error_of(back), provider
        assert (await web.get("/api/me")).status_code == 401
    assert await identities(server) == [] and len(await accounts(server)) == 1


async def test_an_account_squatting_an_email_is_not_joined(
    server: LiveServer, idp: FakeIdP
) -> None:
    await person(server, "ada")  # signed up with this address, never confirmed it
    async with WebClient(server) as web:
        back = await sign_in(web, idp, "google")
        assert "sign in with its password" in error_of(back)
        assert (await web.get("/api/me")).status_code == 401


async def test_state_nonce_and_audience_must_match(server: LiveServer, idp: FakeIdP) -> None:
    async with WebClient(server) as web:
        start = await web.client.get("/api/auth/oauth/google/start")
        callback = idp.authorize(start.headers["location"])
        forged = callback.split("&state=")[0] + "&state=forged"
        assert "not valid" in error_of(await web.client.get(forged))
    async with WebClient(server) as other_browser:  # no flow cookie: someone else's callback
        assert "not valid" in error_of(await other_browser.client.get(callback))
    for claims, problem in (({"nonce": "replayed"}, "does not belong"),
                            ({"aud": "someone-else"}, "another app"),
                            ({"exp": time.time() - 3600}, "expired"),
                            ({"iss": "https://evil.example"}, "another issuer")):  # fmt: skip
        idp.claims = claims
        async with WebClient(server) as web:
            assert problem in error_of(await sign_in(web, idp, "google"))
            assert (await web.get("/api/me")).status_code == 401
    assert await accounts(server) == []


async def test_a_code_works_once(server: LiveServer, idp: FakeIdP) -> None:
    async with WebClient(server) as web:
        start = await web.client.get("/api/auth/oauth/google/start")
        callback = idp.authorize(start.headers["location"])
        cookie = web.client.cookies.get("forge_oauth")
        assert error_of(await web.client.get(callback)) == ""
        web.client.cookies.set("forge_oauth", cookie or "", path="/api/auth/oauth")
        assert error_of(await web.client.get(callback))  # the provider refuses a used code


async def test_after_sign_in_only_paths_of_this_site(server: LiveServer, idp: FakeIdP) -> None:
    for asked, landed in (("/projects/abc", "/projects/abc"), ("https://evil.example", "/"),
                          ("//evil.example", "/"), ("/\\evil.example", "/")):  # fmt: skip
        async with WebClient(server) as web:
            back = await sign_in(web, idp, "google", next=asked)
            assert back.headers["location"] == landed


async def test_invite_mode_needs_an_invite(tmp_path: Path, idp: FakeIdP) -> None:
    with live_server(settings(tmp_path / "data", signup="invite"), idp) as live:
        async with WebClient(live) as web:
            assert "invite" in error_of(await sign_in(web, idp, "google"))
        from forge_web.auth import onetime

        token = await onetime.issue(live.services.db, "invite", email="ada@example.com",
                                    role="admin")  # fmt: skip
        async with WebClient(live) as web:
            back = await sign_in(web, idp, "google", invite=token)
            assert error_of(back) == ""
            assert (await web.get("/api/me")).json()["role"] == "admin"


async def test_disabled_accounts_cannot_sign_in(server: LiveServer, idp: FakeIdP) -> None:
    await person(server, "ada", email_verified=True, status="disabled")
    async with WebClient(server) as web:
        assert "disabled" in error_of(await sign_in(web, idp, "google"))


async def test_a_provider_sign_in_still_asks_for_the_second_factor(
    server: LiveServer, idp: FakeIdP
) -> None:
    secret = new_secret()
    await person(server, "ada", email_verified=True, totp_enabled=True,
                 totp_secret=server.services.vault.encrypt(secret))  # fmt: skip
    async with WebClient(server) as web:
        back = await sign_in(web, idp, "google")
        assert back.status_code == 302 and back.headers["location"] == "/login?second_factor=1"
        assert (await web.get("/api/me")).status_code == 401  # no session before the code
        code = totp(secret_bytes(secret), time.time())
        assert (await web.post("/api/auth/totp/verify", {"code": code})).status_code == 200
        assert (await web.get("/api/me")).json()["email"] == "ada@example.com"


async def test_linking_and_unlinking(server: LiveServer, idp: FakeIdP) -> None:
    bob = await person(server, "bob", password_hash="x")  # GitHub knows him by another email
    assert (await bob.web.client.get("/api/auth/oauth/github/start?intent=link")).status_code == 302
    async with WebClient(server) as stranger:
        start = await stranger.client.get("/api/auth/oauth/github/start?intent=link")
        assert start.status_code == 401
    back = await sign_in(bob.web, idp, "github", intent="link", next="/settings")
    assert back.headers["location"] == "/settings", error_of(back)
    linked = (await bob.web.get("/api/auth/identities")).json()
    assert [(i["provider"], i["label"], i["username"]) for i in linked] == [
        ("github", "GitHub", "ada-gh")
    ]
    carol = await person(server, "carol")
    taken = await sign_in(carol.web, idp, "github", intent="link", next="/settings")
    assert "another user" in error_of(taken)
    assert (await bob.web.request("DELETE", "/api/auth/identities/github")).status_code == 204
    assert (await bob.web.get("/api/auth/identities")).json() == []
    await sign_in(carol.web, idp, "github", intent="link")
    only_way = await carol.web.request("DELETE", "/api/auth/identities/github")
    assert only_way.status_code == 409  # carol has no password


async def test_the_repository_grant_is_stored_encrypted(server: LiveServer, idp: FakeIdP) -> None:
    ada = await person(server, "ada")
    start = await ada.web.client.get("/api/auth/oauth/github/start?intent=repos")
    scopes = parse_qs(urlsplit(start.headers["location"]).query)["scope"][0].split()
    assert "repo" in scopes
    assert (
        await ada.web.client.get("/api/auth/oauth/google/start?intent=repos")
    ).status_code == 404
    back = await ada.web.client.get(idp.authorize(start.headers["location"]))
    assert error_of(back) == ""
    async with server.services.db.session() as session:
        row = await session.scalar(select(GitCredential))
    assert row is not None and row.host == "github.com" and row.username == "ada-gh"
    assert "gho_access" not in row.secret
    assert server.services.vault.decrypt(row.secret) == "gho_access"
    listed = (await ada.web.get("/api/git/credentials")).json()
    assert listed[0]["source"] == "oauth" and "gho_access" not in json.dumps(listed)
    assert await identities(server) == []  # a repository grant is not a sign-in


async def test_personal_tokens(server: LiveServer) -> None:
    ada, bob = await person(server, "ada"), await person(server, "bob")
    body = {"host": "GitLab.com", "username": "ada", "token": "glpat-0123456789"}
    added = await ada.web.post("/api/git/credentials", body)
    assert added.status_code == 201 and added.json()["hint"] == "6789"
    assert "glpat" not in added.text
    bad = {**body, "host": "http://gitlab.com/"}
    assert (await ada.web.post("/api/git/credentials", bad)).status_code == 422
    path = f"/api/git/credentials/{added.json()['id']}"
    assert (await bob.web.request("DELETE", path)).status_code == 404
    assert (await bob.web.get("/api/git/credentials")).json() == []
    assert (await ada.web.request("DELETE", path)).status_code == 204


async def test_openid_connect_with_a_trusted_issuer(tmp_path: Path, idp: FakeIdP) -> None:
    with live_server(settings(tmp_path / "data", trust_email=True), idp) as live:
        async with WebClient(live) as web:
            start = await web.client.get("/api/auth/oauth/firma/start")
            assert start.headers["location"].startswith("https://sso.example.com/auth?")
            back = await web.client.get(idp.authorize(start.headers["location"]))
            assert error_of(back) == ""
            assert (await web.get("/api/me")).json()["email"] == "ada@example.com"
