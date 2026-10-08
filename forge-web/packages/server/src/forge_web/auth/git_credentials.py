"""A user's tokens for git hosts: the GitHub repository grant and personal access tokens.

They are stored encrypted and only ever given to the throwaway git container that clones,
pulls or pushes (W11), never to a project's container.
"""

import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import GitCredential
from forge_web.services import Services, services_of

HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d{1,5})?$")


class CredentialIn(BaseModel):
    """A personal access token for a git host."""

    host: str = Field(default="github.com", max_length=255)
    username: str = Field(default="", max_length=200)
    token: str = Field(min_length=8, max_length=4096)


def credential_view(row: GitCredential) -> dict[str, Any]:
    """A stored credential without its secret."""
    return {"id": row.id, "host": row.host, "username": row.username, "hint": row.hint,
            "source": row.source, "scopes": row.scopes, "created_at": row.created_at}  # fmt: skip


async def save_credential(
    services: Services,
    user_id: str,
    host: str,
    username: str,
    token: str,
    *,
    source: str,
    scopes: str = "",
) -> GitCredential:
    """Store a token for a host (replacing the user's earlier one for that host)."""
    row = GitCredential(
        id=secrets.token_hex(16), user_id=user_id, host=host, username=username,
        secret=services.vault.encrypt(token), hint=token[-4:], source=source,
        scopes=scopes[:500], created_at=time.time(),
    )  # fmt: skip
    async with services.db.session() as session, session.begin():
        await session.execute(
            delete(GitCredential).where(
                GitCredential.user_id == user_id, GitCredential.host == host
            )
        )
        session.add(row)
    return row


async def credential_for(services: Services, user_id: str, host: str) -> tuple[str, str] | None:
    """The user's username and token for a host, or None."""
    async with services.db.session() as session:
        row = await session.scalar(
            select(GitCredential).where(
                GitCredential.user_id == user_id, GitCredential.host == host
            )
        )
    return (row.username, services.vault.decrypt(row.secret)) if row is not None else None


def git_credentials_router() -> APIRouter:
    """`/api/git/credentials`: list, add and remove the signed-in user's tokens."""
    router = APIRouter(prefix="/api/git/credentials")

    @router.get("")
    async def credentials(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        query = select(GitCredential).where(GitCredential.user_id == user.id)
        async with services_of(request).db.session() as session:
            return [credential_view(row) for row in await session.scalars(query)]

    @router.post("", status_code=201)
    async def add(body: CredentialIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        host = body.host.strip().lower()
        if not HOST.match(host):
            raise HTTPException(422, "the host must be a name like github.com")
        row = await save_credential(services, user.id, host, body.username.strip(),
                                    body.token.strip(), source="token")  # fmt: skip
        await audit(services.db, "git_token_added", user_id=user.id, ip=client_ip(request),
                    host=host)  # fmt: skip
        return credential_view(row)

    @router.delete("/{credential_id}", status_code=204)
    async def remove(credential_id: str, request: Request, user: CurrentUser) -> None:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            result = await session.execute(
                delete(GitCredential).where(
                    GitCredential.user_id == user.id, GitCredential.id == credential_id
                )
            )
        if not result.rowcount:  # type: ignore[attr-defined]
            raise HTTPException(404, "no such credential")

    return router
