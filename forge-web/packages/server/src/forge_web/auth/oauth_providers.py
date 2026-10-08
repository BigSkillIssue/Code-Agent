"""The identity providers' side of signing in: authorization URLs, code exchange, who the user is.

Google and other OpenID Connect issuers send an ID token; it comes straight from the issuer's
token endpoint over TLS, so its claims are checked (issuer, audience, expiry, nonce) without
checking its signature, as OpenID Connect Core 3.1.3.7 allows. GitHub has no ID token: the
user and their verified primary email come from its API.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from forge_web.settings import ProviderSettings

log = logging.getLogger(__name__)
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CLOCK_SKEW = 120  # seconds an ID token's times may be off


class ProviderError(Exception):
    """Signing in with a provider failed; the message is safe to show."""


@dataclass
class Endpoints:
    """Where a provider signs people in."""

    authorize: str
    token: str
    issuers: tuple[str, ...] = ()  # OpenID Connect: accepted `iss` values
    userinfo: str = ""


@dataclass
class Identity:
    """Who signed in, as the provider says."""

    provider: str
    subject: str  # the provider's stable user id
    email: str
    email_verified: bool
    name: str = ""
    username: str = ""
    avatar_url: str = ""


@dataclass
class Tokens:
    """What the token endpoint answered."""

    access_token: str
    id_token: str = ""
    scope: str = ""


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT (not its signature: see the module docstring)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise ProviderError("the provider sent an unreadable ID token") from None
    if not isinstance(data, dict):
        raise ProviderError("the provider sent an unreadable ID token")
    return data


def claims_problem(claims: dict[str, Any], issuers: tuple[str, ...], client_id: str,
                   nonce: str, now: float) -> str | None:  # fmt: skip
    """Why an ID token's claims are not acceptable, or None."""
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") not in issuers:
        return "the ID token comes from another issuer"
    if client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != client_id):
        return "the ID token is meant for another app"
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] < now - CLOCK_SKEW:
        return "the ID token has expired"
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        return "the ID token does not belong to this sign-in"
    if not claims.get("sub"):
        return "the ID token names no user"
    return None


def truthy(value: Any) -> bool:
    """`email_verified` as some issuers send it (a bool, or the text "true")."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


class Provider:
    """One configured sign-in provider."""

    def __init__(self, name: str, settings: ProviderSettings, secret: str) -> None:
        self.name = name
        self.settings = settings
        self.kind = settings.provider_kind(name)
        self.label = settings.label or {"google": "Google", "github": "GitHub"}.get(
            name, name.replace("-", " ").title()
        )
        self.client_id = settings.client_id
        self.secret = secret
        self._endpoints: Endpoints | None = None

    @property
    def github_api(self) -> str:
        """The GitHub API base URL (github.com or GitHub Enterprise)."""
        base = self.settings.url.rstrip("/")
        return f"{base}/api/v3" if base else "https://api.github.com"

    @property
    def git_host(self) -> str:
        """The git host this provider's repository grants are for."""
        host = urlsplit(self.settings.url).hostname if self.settings.url else None
        return host or "github.com"

    def scopes(self, repos: bool = False) -> list[str]:
        """The scopes to ask for."""
        base = {"github": ["read:user", "user:email"]}.get(
            self.kind, ["openid", "email", "profile"]
        )
        if repos:
            base = [*base, "repo"]
        return [*base, *(s for s in self.settings.scopes if s not in base)]

    async def endpoints(self, http: httpx.AsyncClient) -> Endpoints:
        """The provider's endpoints (read once from OpenID discovery for generic issuers)."""
        if self._endpoints is not None:
            return self._endpoints
        if self.kind == "google":
            self._endpoints = Endpoints(
                "https://accounts.google.com/o/oauth2/v2/auth",
                "https://oauth2.googleapis.com/token",
                GOOGLE_ISSUERS,
            )
        elif self.kind == "github":
            web = self.settings.url.rstrip("/") or "https://github.com"
            self._endpoints = Endpoints(
                f"{web}/login/oauth/authorize", f"{web}/login/oauth/access_token"
            )
        else:
            self._endpoints = await self._discover(http)
        return self._endpoints

    async def _discover(self, http: httpx.AsyncClient) -> Endpoints:
        issuer = self.settings.issuer.rstrip("/")
        if not issuer.startswith("https://"):
            raise ProviderError(f"{self.label}: the issuer must be an https:// URL")
        data = await fetch_json(http, "GET", f"{issuer}/.well-known/openid-configuration")
        try:
            return Endpoints(
                str(data["authorization_endpoint"]),
                str(data["token_endpoint"]),
                (str(data.get("issuer") or issuer),),
                str(data.get("userinfo_endpoint") or ""),
            )
        except KeyError:
            raise ProviderError(f"{self.label}: the issuer's discovery is incomplete") from None

    def authorize_url(self, endpoints: Endpoints, redirect_uri: str, state: str,
                      challenge: str, nonce: str, repos: bool = False) -> str:  # fmt: skip
        """Where the browser goes to sign in."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes(repos)),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.kind != "github":
            params["nonce"] = nonce
        if self.kind == "google":
            params["prompt"] = "select_account"
        return f"{endpoints.authorize}?{urlencode(params)}"

    async def exchange(self, http: httpx.AsyncClient, endpoints: Endpoints, code: str,
                       redirect_uri: str, verifier: str) -> Tokens:  # fmt: skip
        """Trade the authorization code for tokens."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.secret,
            "code_verifier": verifier,
        }
        data = await fetch_json(http, "POST", endpoints.token, data=form)
        if "error" in data or not data.get("access_token"):
            log.info("%s token exchange failed: %s", self.name, data.get("error"))
            raise ProviderError(f"{self.label} did not accept the sign-in; please try again")
        return Tokens(str(data["access_token"]), str(data.get("id_token") or ""),
                      str(data.get("scope") or ""))  # fmt: skip

    async def identity(self, http: httpx.AsyncClient, endpoints: Endpoints, tokens: Tokens,
                       nonce: str) -> Identity:  # fmt: skip
        """Who signed in."""
        if self.kind == "github":
            return await self._github_identity(http, tokens.access_token)
        if not tokens.id_token:
            raise ProviderError(f"{self.label} sent no ID token")
        claims = jwt_claims(tokens.id_token)
        problem = claims_problem(claims, endpoints.issuers, self.client_id, nonce, time.time())
        if problem:
            raise ProviderError(f"{self.label}: {problem}")
        if not claims.get("email") and endpoints.userinfo:
            info = await fetch_json(http, "GET", endpoints.userinfo, token=tokens.access_token)
            if info.get("sub") == claims["sub"]:
                claims = {**info, **claims, "email": info.get("email"),
                          "email_verified": info.get("email_verified")}  # fmt: skip
        verified = truthy(claims.get("email_verified")) or (
            self.kind == "oidc" and self.settings.trust_email and bool(claims.get("email"))
        )
        return Identity(
            provider=self.name, subject=str(claims["sub"]),
            email=str(claims.get("email") or "").strip().lower(), email_verified=verified,
            name=str(claims.get("name") or ""), avatar_url=str(claims.get("picture") or ""),
            username=str(claims.get("preferred_username") or ""),
        )  # fmt: skip

    async def _github_identity(self, http: httpx.AsyncClient, token: str) -> Identity:
        user = await fetch_json(http, "GET", f"{self.github_api}/user", token=token)
        emails = await fetch_json(http, "GET", f"{self.github_api}/user/emails", token=token)
        chosen = next(
            (e for e in emails.get("items", []) if e.get("primary") and e.get("verified")), None
        )
        if not user.get("id"):
            raise ProviderError("GitHub did not say who you are")
        return Identity(
            provider=self.name, subject=str(user["id"]),
            email=str(chosen["email"]).strip().lower() if chosen else "",
            email_verified=chosen is not None, name=str(user.get("name") or ""),
            username=str(user.get("login") or ""), avatar_url=str(user.get("avatar_url") or ""),
        )  # fmt: skip


async def fetch_json(http: httpx.AsyncClient, method: str, url: str, *, token: str = "",
                     data: dict[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Call a provider endpoint; lists come back as {"items": [...]}."""
    headers = {"Accept": "application/json", "User-Agent": "forge-web"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = await http.request(method, url, headers=headers, data=data)
        value = response.json()
    except (httpx.HTTPError, ValueError) as err:
        log.warning("%s %s failed: %s", method, url, err)
        raise ProviderError("the sign-in provider could not be reached; please try again") from None
    if isinstance(value, list):
        return {"items": value}
    if not isinstance(value, dict):
        raise ProviderError("the sign-in provider sent an unexpected answer")
    if response.status_code >= 400 and "error" not in value:
        value["error"] = f"HTTP {response.status_code}"
    return value


def load_providers(configured: dict[str, ProviderSettings],
                   environ: dict[str, str] | None = None) -> dict[str, Provider]:  # fmt: skip
    """The providers whose client secret is set (the others are skipped with a warning)."""
    env = dict(os.environ) if environ is None else environ
    found: dict[str, Provider] = {}
    for name, settings in configured.items():
        variable = (
            settings.client_secret_env or f"FORGE_WEB_{name.upper().replace('-', '_')}_SECRET"
        )
        secret = env.get(variable) or settings.client_secret
        if not secret or not settings.client_id:
            log.warning("sign-in with %s is off: set its client secret in %s", name, variable)
            continue
        if settings.provider_kind(name) == "oidc" and not settings.issuer:
            log.warning("sign-in with %s is off: it needs an issuer URL", name)
            continue
        found[name] = Provider(name, settings, secret)
    return found


class SignIn:
    """The configured providers and the HTTP client that talks to them."""

    def __init__(self, providers: dict[str, Provider], http: httpx.AsyncClient | None = None):
        self.providers = providers
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False)

    def buttons(self) -> list[dict[str, str]]:
        """What the sign-in page shows."""
        return [{"name": p.name, "label": p.label} for p in self.providers.values()]

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.http.aclose()
