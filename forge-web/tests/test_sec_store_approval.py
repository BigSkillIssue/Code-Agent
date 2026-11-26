"""SEC-W5: what the sandbox can make the server do with `/product/store/publish` (W58).

Agent publishing is the user's decision (admin's `stores.agent_publishing`, the plan's store
publishing, a passing review of the commit). Its documented boundary: Google Play's production
keeps the admin's approval for people who are not trusted, also when the admin's Forge account
publishes for them; and a publish that does not start leaves no go-live approval behind."""

import secrets
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.store_models import AndroidBuild, PlayKey, PlayRelease
from support import LiveServer, person
from test_agent_product import project_with_product, run_header
from test_hosting_deploy import settings_for, short_polls  # noqa: F401 (autouse fixture)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        live.services.gateway.chat_is_working = lambda _chat_id: True
        live.services.settings.stores.agent_publishing = True
        yield live


@pytest.fixture
async def gateway(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.AsyncHTTPTransport(uds=str(server.services.gateway_server.path))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as client:
        yield client


async def android_build(server: LiveServer, project_id: str, commit: str, package: str) -> None:
    now = time.time()
    row = AndroidBuild(id=secrets.token_hex(16), project_id=project_id,
                       user_id=server.services.dev_user_id, commit=commit, version_code=1,
                       version_name="1.0", package_name=package, status="built", created_at=now,
                       updated_at=now)  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        session.add(row)
    bundle = server.services.android.bundle(row)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"PK")


async def forge_account(server: LiveServer) -> None:
    admin_id = server.services.dev_user_id
    async with server.services.db.session() as session, session.begin():
        session.add(PlayKey(user_id=admin_id, client_email="forge@example.iam", secret="sealed",
                            account="organization", created_at=time.time()))  # fmt: skip
    server.services.settings.stores.forge_account = admin_id


async def reviewed(gateway: httpx.AsyncClient, headers: dict[str, str], head: str) -> None:
    review = {"store": "google_play", "commit": head, "verdict": "pass", "summary": "ok"}
    assert (
        await gateway.post("/product/store/review", headers=headers, json=review)
    ).status_code == 200


async def test_the_forge_account_does_not_skip_the_admins_approval_for_untrusted_people(
    server: LiveServer, gateway: httpx.AsyncClient
) -> None:
    launched: list[str] = []
    server.services.play.launch = launched.append  # type: ignore[method-assign]
    maria = await person(server, "maria")  # neither an admin nor trusted for hosting
    project_id, chat, head = await project_with_product(server, maria.web, "Garten")
    await android_build(server, project_id, head, "com.example.garten")
    await forge_account(server)  # the admin's own account, which is trusted
    headers = run_header(server, chat)
    await reviewed(gateway, headers, head)
    publish = {"store": "google_play", "commit": head, "track": "production"}
    done = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert done.status_code == 200, done.text
    assert done.json()["started"]["status"] == "waiting"  # for the admin, as for Maria herself
    assert launched == []
    async with server.services.db.session() as session:
        release = await session.scalar(select(PlayRelease))
    assert release is not None and release.status == "waiting" and release.admin == ""


async def test_a_publish_that_does_not_start_leaves_no_go_live_approval(
    server: LiveServer, gateway: httpx.AsyncClient
) -> None:
    maria = await person(server, "maria")
    project_id, chat, head = await project_with_product(server, maria.web, "Wiese")
    await android_build(server, project_id, head, "")  # names no applicationId: refused
    await forge_account(server)
    headers = run_header(server, chat)
    await reviewed(gateway, headers, head)
    publish = {"store": "google_play", "commit": head, "track": "internal"}
    refused = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert refused.status_code == 409, refused.text
    async with server.services.db.session() as session:
        approvals = list(await session.scalars(select(GoLiveApproval.id)))
    assert approvals == []  # nothing approved in Maria's name that unlocks other releases
