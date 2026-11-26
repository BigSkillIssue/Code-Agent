"""Forge's agent and the stores (W58): the guidelines the admin loaded, the store review, and
publishing through the stores' APIs only after a passing review, with the person's own account or
the admin's Forge account."""

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.models import AppleApproval, AppStoreKey, AuditEntry
from support import LiveServer
from test_agent_product import project_with_product, run_header
from test_hosting_deploy import settings_for, short_polls  # noqa: F401 (autouse fixture)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        live.services.gateway.chat_is_working = lambda _chat_id: True
        yield live


@pytest.fixture
async def gateway(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.AsyncHTTPTransport(uds=str(server.services.gateway_server.path))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as client:
        yield client


async def store_key(server: LiveServer, user_id: str) -> None:
    async with server.services.db.session() as session, session.begin():
        session.add(AppStoreKey(user_id=user_id, key_id="K1", issuer_id="i", team_id="T1",
                                secret="sealed", created_at=time.time()))  # fmt: skip


async def test_publishing_needs_the_admins_switch_a_passing_review_and_an_account(
    server: LiveServer, gateway: httpx.AsyncClient
) -> None:
    started: list[str] = []
    server.services.releases.launch = started.append  # type: ignore[method-assign]
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as web:
        guidelines = {"apple": "4.2 Minimum Functionality ...", "google_play": ""}
        saved = await web.request("PUT", "/api/admin/store-guidelines", json=guidelines)
        assert saved.status_code == 200 and saved.json() == {"apple": guidelines["apple"]}
        project_id, chat, head = await project_with_product(server, web, "Lehm")
    headers = run_header(server, chat)
    loaded = (await gateway.get("/product/store/guidelines/apple", headers=headers)).json()
    assert loaded["text"].startswith("4.2") and not loaded["missing"]
    assert (await gateway.get("/product/store/guidelines/google_play", headers=headers)).json()[
        "missing"
    ]
    publish = {"store": "apple", "commit": head, "platforms": ["ios"]}
    off = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert off.status_code == 403  # the admin did not let the agent publish
    server.services.settings.stores.agent_publishing = True
    unreviewed = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert unreviewed.status_code == 409 and "store review" in unreviewed.text
    concern = {"store": "apple", "commit": head, "verdict": "concern", "summary": "login wall"}
    assert (
        await gateway.post("/product/store/review", headers=headers, json=concern)
    ).status_code == 200
    assert (
        await gateway.post("/product/store/publish", headers=headers, json=publish)
    ).status_code == 409
    passing = {**concern, "verdict": "pass", "summary": "meets 4.2 and 5.1.1"}
    assert (
        await gateway.post("/product/store/review", headers=headers, json=passing)
    ).status_code == 200
    no_account = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert no_account.status_code == 409 and "App Store Connect key" in no_account.text
    admin_id = server.services.dev_user_id
    await store_key(server, admin_id)  # the dev user is the admin: its own key publishes
    done = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert done.status_code == 200, done.text
    assert done.json()["started"][0]["platform"] == "ios" and len(started) == 1
    async with server.services.db.session() as session:
        approval = await session.scalar(select(AppleApproval))
        actions = set(await session.scalars(select(AuditEntry.action)))
    assert approval is not None and approval.commit == head
    assert approval.request_id.startswith("store-review:") and approval.project_id == project_id
    assert {"agent.store_review", "agent.store_publish"} <= actions


async def test_the_forge_account_publishes_for_people_without_their_own(
    server: LiveServer, gateway: httpx.AsyncClient
) -> None:
    from support import person

    started: list[str] = []
    server.services.releases.launch = started.append  # type: ignore[method-assign]
    server.services.settings.stores.agent_publishing = True
    maria = await person(server, "maria")
    _, chat, head = await project_with_product(server, maria.web, "Garten")
    headers = run_header(server, chat)
    review = {"store": "apple", "commit": head, "verdict": "pass", "summary": "ok"}
    await gateway.post("/product/store/review", headers=headers, json=review)
    publish: dict[str, Any] = {"store": "apple", "commit": head}
    assert (
        await gateway.post("/product/store/publish", headers=headers, json=publish)
    ).status_code == 409
    admin_id = server.services.dev_user_id
    await store_key(server, admin_id)
    server.services.settings.stores.forge_account = admin_id
    done = await gateway.post("/product/store/publish", headers=headers, json=publish)
    assert done.status_code == 200, done.text
    assert len(started) == 1  # with the admin's Forge account
