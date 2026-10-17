"""What being an admin allows outside the admin pages follows the two-factor rule too (W42): an
admin without a second factor on a server that asks for one cannot open server folders or use
the server's AI keys without a grant (and their releases and deploys wait for an admin, through
the same `acts_as_admin`); removing a git token and withdrawing an invite are audited."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from forge_web.auth.admin_role import acts_as_admin
from forge_web.db.models import AuditEntry, User
from forge_web.gateway.keys import server_key_limit
from forge_web.settings import GatewaySettings
from support import LiveServer, dev_settings


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        yield c


async def actions(server: LiveServer) -> list[str]:
    async with server.services.db.session() as session:
        return list(await session.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))


async def test_an_admin_without_a_second_factor_loses_the_admins_shortcuts(
    server: LiveServer, admin: httpx.AsyncClient, tmp_path: Path
) -> None:
    folder = {"name": "Server folder", "source": "folder", "folder": str(tmp_path)}
    server.services.settings.auth.admin_two_factor = True
    refused = await admin.post("/api/projects", json=folder)
    assert refused.status_code == 403 and "two-factor" in refused.text
    async with server.services.db.session() as session:
        user = await session.get(User, server.services.dev_user_id)
    assert user is not None and user.role == "admin" and not user.totp_enabled
    assert not acts_as_admin(user, True) and acts_as_admin(user, False)
    granted_only = GatewaySettings(server_keys_for="granted", monthly_limit_usd=50.0)
    assert await server_key_limit(server.services.db, user, granted_only, True) is None
    assert await server_key_limit(server.services.db, user, granted_only, False) == 50.0
    user.totp_enabled = True  # with the second factor the admin is an admin again
    assert acts_as_admin(user, True)


async def test_removing_a_git_token_and_withdrawing_an_invite_are_audited(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    made = await admin.post("/api/git/credentials", json={"host": "github.com", "token": "t" * 12})
    assert made.status_code == 201, made.text
    assert (await admin.delete(f"/api/git/credentials/{made.json()['id']}")).status_code == 204
    invited = await admin.post("/api/admin/invites", json={"email": "ada@example.com"})
    assert invited.status_code == 201
    [invite] = (await admin.get("/api/admin/invites")).json()
    assert (await admin.delete(f"/api/admin/invites/{invite['id']}")).status_code == 204
    assert (await admin.get("/api/admin/invites")).json() == []
    done = await actions(server)
    assert "git_token_removed" in done and "invite_withdrawn" in done
    assert done.index("git_token_removed") > done.index("git_token_added")
