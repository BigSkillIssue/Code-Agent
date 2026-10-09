"""Signing in with Google, GitHub or another OpenID Connect provider: the browser's side.

`/start` remembers the attempt in a signed, HttpOnly cookie (state, PKCE verifier, nonce, where
to go afterwards) and sends the browser to the provider; `/callback` accepts only the answer to
that same attempt in that same browser. A provider account is linked to an existing Forge
account only through an email the provider has verified, or by a signed-in user on purpose.
"""

import base64
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select

from forge_web.audit import audit
from forge_web.auth.accounts import refuse_inactive
from forge_web.auth.git_credentials import save_credential
from forge_web.auth.oauth_providers import Identity as ProviderIdentity
from forge_web.auth.oauth_providers import Provider, ProviderError, pkce_pair
from forge_web.auth.routes import find_user, new_account
from forge_web.auth.second_factor import finish_sign_in
from forge_web.auth.sessions import (
    CurrentUser,
    client_ip,
    is_https,
    load_session,
    session_token,
)
from forge_web.db.models import Identity, User
from forge_web.services import Services, services_of
from forge_web.vault import sign

FLOW_COOKIE = "forge_oauth"
FLOW_PATH = "/api/auth/oauth"
FLOW_SECONDS = 600
Intent = Literal["login", "link", "repos"]


@dataclass
class Flow:
    """One sign-in attempt, kept in the browser's flow cookie."""

    provider: str
    state: str
    verifier: str
    nonce: str
    intent: str
    next: str
    user_id: str = ""  # link and repos: who started it
    invite: str = ""
    expires: float = 0.0


def flow_key(services: Services) -> bytes:
    """The key that signs flow cookies."""
    return services.vault.derive("oauth-flow")


def seal(key: bytes, flow: Flow) -> str:
    """The flow as a signed cookie value."""
    payload = base64.urlsafe_b64encode(json.dumps(asdict(flow)).encode()).decode().rstrip("=")
    return f"{payload}.{sign(key, payload)}"


def unseal(key: bytes, value: str | None) -> Flow | None:
    """The flow from a cookie value, if it is signed by us and not expired."""
    payload, _, signature = (value or "").rpartition(".")
    if not payload or not secrets.compare_digest(sign(key, payload), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        flow = Flow(**data)
    except (ValueError, TypeError):
        return None
    return flow if flow.expires > time.time() else None


def safe_next(path: str) -> str:
    """A path on this site to go to afterwards (never another site)."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def redirect_uri(services: Services, name: str) -> str:
    """Where the provider sends the browser back to (register this URL with the provider)."""
    return f"{services.settings.base_url()}{FLOW_PATH}/{name}/callback"


def failed(message: str, flow: Flow | None = None) -> Response:
    """Back to the app with an error the page shows."""
    target = flow.next if flow is not None and flow.intent != "login" else "/"
    joiner = "&" if "?" in target else "?"
    response = RedirectResponse(f"{target}{joiner}auth_error={quote(message)}", status_code=302)
    response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
    return response


def oauth_router() -> APIRouter:
    """`/api/auth/oauth/{provider}/start|callback` and `/api/auth/identities`."""
    router = APIRouter(prefix="/api/auth")
    flow_routes(router)
    identity_routes(router)
    return router


def flow_routes(router: APIRouter) -> None:
    """Start a sign-in and accept its answer."""

    @router.get("/oauth/{name}/start")
    async def start(
        name: str,
        request: Request,
        intent: Intent = "login",
        next_path: str = Query("/", alias="next", max_length=500),
        invite: str = Query("", max_length=200),
    ) -> Response:
        services = services_of(request)
        provider = services.sign_in.providers.get(name)
        if provider is None or (intent == "repos" and provider.kind != "github"):
            raise HTTPException(404, "no such sign-in provider")
        user_id = ""
        if intent != "login":
            found = await load_session(services, session_token(request.cookies))
            if found is None:
                raise HTTPException(401, "sign in first")
            user_id = found[1].id
        try:
            endpoints = await provider.endpoints(services.sign_in.http)
        except ProviderError as err:
            return failed(str(err))
        verifier, challenge = pkce_pair()
        flow = Flow(name, secrets.token_urlsafe(24), verifier, secrets.token_urlsafe(24), intent,
                    safe_next(next_path), user_id, invite, time.time() + FLOW_SECONDS)  # fmt: skip
        url = provider.authorize_url(endpoints, redirect_uri(services, name), flow.state,
                                     challenge, flow.nonce, repos=intent == "repos")  # fmt: skip
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            FLOW_COOKIE, seal(flow_key(services), flow), max_age=FLOW_SECONDS, httponly=True,
            secure=is_https(services), samesite="lax", path=FLOW_PATH,
        )  # fmt: skip
        return response

    @router.get("/oauth/{name}/callback")
    async def callback(
        name: str, request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Response:
        services = services_of(request)
        flow = unseal(flow_key(services), request.cookies.get(FLOW_COOKIE))
        provider = services.sign_in.providers.get(name)
        if (
            provider is None
            or flow is None
            or flow.provider != name
            or not secrets.compare_digest(flow.state, state)
        ):
            return failed("this sign-in attempt is not valid any more; please start again")
        if error or not code:
            return failed("the sign-in was cancelled", flow)
        if not services.limits.oauth_by_ip.allow(client_ip(request)):
            return failed("too many attempts; try again later", flow)
        try:
            return await finish(request, services, provider, flow, code)
        except ProviderError as err:
            return failed(str(err), flow)
        except HTTPException as err:
            return failed(str(err.detail), flow)


async def finish(
    request: Request, services: Services, provider: Provider, flow: Flow, code: str
) -> Response:
    """Trade the code, find out who it is, and sign in, link or store the repository grant."""
    http = services.sign_in.http
    endpoints = await provider.endpoints(http)
    tokens = await provider.exchange(
        http, endpoints, code, redirect_uri(services, provider.name), flow.verifier
    )
    identity = await provider.identity(http, endpoints, tokens, flow.nonce)
    response = RedirectResponse(flow.next, status_code=302)
    response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
    ip = client_ip(request)
    if flow.intent == "login":
        user = await sign_in_user(services, provider, identity, flow.invite)
        if user.totp_enabled:  # the UI asks for the code, then goes on
            response = RedirectResponse("/login?second_factor=1", status_code=302)
            response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
        if await finish_sign_in(request, response, user):
            await audit(services.db, "login", user_id=user.id, ip=ip, method=provider.name)
        return response
    user_id = await same_user(request, services, flow)
    if flow.intent == "repos":
        await save_credential(services, user_id, provider.git_host, identity.username,
                              tokens.access_token, source="oauth", scopes=tokens.scope)  # fmt: skip
        await audit(services.db, "git_connected", user_id=user_id, ip=ip, host=provider.git_host)
    else:
        await link_identity(services, provider, identity, user_id)
        await audit(services.db, "identity_linked", user_id=user_id, ip=ip, method=provider.name)
    return response


async def same_user(request: Request, services: Services, flow: Flow) -> str:
    """The signed-in user, who must be the one who started the flow."""
    found = await load_session(services, session_token(request.cookies))
    if found is None or found[1].id != flow.user_id:
        raise HTTPException(401, "sign in again first")
    return found[1].id


async def sign_in_user(
    services: Services, provider: Provider, identity: ProviderIdentity, invite: str
) -> User:
    """The account of a provider identity: linked before, matched by verified email, or new."""
    now = time.time()
    async with services.db.session() as session, session.begin():
        row = await session.get(Identity, (identity.provider, identity.subject))
        if row is not None:
            row.last_used_at = now
            row.email, row.username = identity.email or row.email, identity.username
            user = await session.get(User, row.user_id)
    if row is None:
        if not identity.email or not identity.email_verified:
            raise HTTPException(403, f"your {provider.label} account has no verified email")
        user = await find_user(services, identity.email)
        if user is not None and (not user.email_verified or user.password_hash is not None):
            # Someone may have signed up with this address before its owner (and knows the
            # password): never join them on the email alone; the owner links it while signed in.
            raise HTTPException(
                403, "an account with this email exists; sign in with its password and link "
                f"{provider.label} in your settings",
            )  # fmt: skip
        if user is None:
            user = await new_account(
                services, identity.email, identity.name, None, invite, verified=True
            )
        await link_identity(services, provider, identity, user.id)
    if user is None:
        raise HTTPException(403, "this account no longer exists")
    refuse_inactive(user)
    return user


async def link_identity(
    services: Services, provider: Provider, identity: ProviderIdentity, user_id: str
) -> None:
    """Link a provider account to a user; 409 if it belongs to someone else."""
    now = time.time()
    async with services.db.session() as session, session.begin():
        row = await session.get(Identity, (identity.provider, identity.subject))
        if row is not None and row.user_id != user_id:
            raise HTTPException(409, f"this {provider.label} account is linked to another user")
        if row is None:
            session.add(Identity(provider=identity.provider, subject=identity.subject,
                                 user_id=user_id, email=identity.email,
                                 username=identity.username, created_at=now,
                                 last_used_at=now))  # fmt: skip


def identity_routes(router: APIRouter) -> None:
    """A user's linked sign-in accounts."""

    @router.get("/identities")
    async def identities(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = services_of(request)
        query = select(Identity).where(Identity.user_id == user.id).order_by(Identity.provider)
        async with services.db.session() as session:
            rows = list(await session.scalars(query))
        labels = {p.name: p.label for p in services.sign_in.providers.values()}
        return [{"provider": r.provider, "label": labels.get(r.provider, r.provider),
                 "email": r.email, "username": r.username, "created_at": r.created_at,
                 "last_used_at": r.last_used_at} for r in rows]  # fmt: skip

    @router.delete("/identities/{provider}", status_code=204)
    async def unlink(provider: str, request: Request, user: CurrentUser) -> None:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            others = await session.scalar(
                select(func.count())
                .select_from(Identity)
                .where(Identity.user_id == user.id, Identity.provider != provider)
            )
            if not others and user.password_hash is None:
                raise HTTPException(409, "this is your only way to sign in; set a password first")
            await session.execute(
                delete(Identity).where(Identity.user_id == user.id, Identity.provider == provider)
            )
        await audit(services.db, "identity_unlinked", user_id=user.id, ip=client_ip(request),
                    method=provider)  # fmt: skip
