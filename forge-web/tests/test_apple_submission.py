"""The App Store step (W22d2), end to end against the stand-ins: a release in TestFlight is made
ready for App Review on the user's click (version, texts, app information, age rating, terms,
review contact, store screenshots), sent to Apple only with a confirmation naming the version,
followed through App Review, and released on another click."""

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from asc_standin import AscStandIn
from forge_web.db.models import AppleApproval, AuditEntry
from support import LiveServer
from test_apple_release import (
    BUNDLE,
    approved_project,
    finished,
    mac_online,
    settings_for,
    until,
)

LISTING = {
    "locale": "en-US", "name": "Tally", "subtitle": "Count anything",
    "description": "Tap to count.", "keywords": "counter,tally", "primary_category": "UTILITIES",
    "copyright": "2026 Ada", "whats_new": "Faster counting.",
    "support_url": "https://example.com/help", "privacy_policy_url": "https://example.com/privacy",
}  # fmt: skip
CONTACT = {"first_name": "Ada", "last_name": "Lovelace", "phone": "+44 20 7946 0000",
           "email": "review@example.com", "notes": "Tap the number to count."}  # fmt: skip


@pytest.fixture
def asc() -> Iterator[AscStandIn]:
    with AscStandIn() as standin:
        standin.state.release.part_size = 64 * 1024
        standin.state.release.polls = 1
        yield standin


@pytest.fixture
def server(tmp_path: Path, asc: AscStandIn) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data", asc)) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def in_testflight(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn
) -> tuple[str, str]:
    """An approved project whose iPhone release reached TestFlight; (project, release)."""
    asc.add_app("Tally", BUNDLE)
    project_id = await approved_project(server, client, asc)
    await client.post(f"/api/projects/{project_id}/apple/releases", json={"platforms": ["ios"]})
    [release] = await until(lambda: finished(client, project_id, 1), 90)
    assert release["status"] == "done", release
    return project_id, str(release["id"])


async def settled(client: httpx.AsyncClient, url: str, wanted: tuple[str, ...]) -> Any:
    [found] = (await client.get(url)).json()
    return found if found["status"] in wanted else None


async def test_from_testflight_through_app_review_to_the_app_store(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn, tmp_path: Path
) -> None:
    async with mac_online(client, server.url, tmp_path / "mac"):
        project_id, release_id = await in_testflight(server, client, asc)
        url = f"/api/projects/{project_id}/apple/submissions"
        body = {"release_id": release_id, "contact": CONTACT}
        no_texts = await client.post(url, json=body)
        assert no_texts.status_code == 409 and "store texts" in no_texts.text
        saved = await client.put(f"/api/projects/{project_id}/apple/listing", json=LISTING)
        assert saved.status_code == 200, saved.text
        prepared = await client.post(url, json=body)
        assert prepared.status_code == 200, prepared.text
        ready = await until(lambda: settled(client, url, ("ready", "failed")), 90)
    assert ready["status"] == "ready", ready
    submit = asc.state.submit
    [(version_id, version)] = submit.versions.items()
    assert version["attributes"]["versionString"] == "1.0" and version["build"]
    assert version["attributes"]["releaseType"] == "MANUAL"  # released only on the user's click
    [text] = [asc.state.store.localizations[lid]["attributes"] for lid in version["localizations"]]
    assert text["description"] == "Tap to count." and "whatsNew" not in text  # a first version
    info = next(iter(submit.infos.values()))
    assert info["categories"] == {"primaryCategory": "UTILITIES"} and info["age"]
    assert info["age"]["violenceRealistic"] == "NONE" and info["age"]["gambling"] is False
    [names] = info["localizations"].values()
    assert names["name"] == "Tally" and names["privacyPolicyUrl"] == LISTING["privacy_policy_url"]
    terms = next(iter(submit.app_terms.values()))
    assert terms["contentRightsDeclaration"] == "DOES_NOT_USE_THIRD_PARTY_CONTENT"
    assert terms["price"] == "pp-1" and terms["territories"] == ["USA", "DEU", "FRA", "JPN"]
    assert version["review"]["contactLastName"] == "Lovelace"
    groups = sorted(p["group"] for p in asc.state.store.placements.values())
    assert (
        groups
        == ["IPAD_PRO_13_PROFILE"] * 2
        + ["IPHONE_DYNAMIC_ISLAND_LARGE_PROFILE"] * 2
        + ["WATCH_ULTRA_PROFILE"] * 2
    )  # light and dark, every device
    assert submit.submissions == {}  # nothing went to App Review yet
    send = f"{url}/{ready['id']}/submit"
    assert (await client.post(send, json={"confirm": False, "version": "1.0"})).status_code == 422
    wrong = await client.post(send, json={"confirm": True, "version": "2.0"})
    assert wrong.status_code == 409
    sent = await client.post(send, json={"confirm": True, "version": "1.0"})
    assert sent.status_code == 200, sent.text
    decided = await until(lambda: settled_state(client, url), 30)
    assert decided["version_state"] == "PENDING_DEVELOPER_RELEASE"
    released = await client.post(f"{url}/{ready['id']}/release")
    assert released.status_code == 200, released.text
    assert submit.released == [version_id] and released.json()["status"] == "released"
    async with server.services.db.session() as session:
        actions = [a.action for a in await session.scalars(select(AuditEntry))]
    for action in ("apple.submission_prepared", "apple.submitted", "apple.released"):
        assert action in actions


async def settled_state(client: httpx.AsyncClient, url: str) -> Any:
    [found] = (await client.get(url)).json()
    return found if found["version_state"] == "PENDING_DEVELOPER_RELEASE" else None


async def test_only_the_newest_approval_goes_to_apple(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn, tmp_path: Path
) -> None:
    async with mac_online(client, server.url, tmp_path / "mac"):
        project_id, release_id = await in_testflight(server, client, asc)
    await client.put(f"/api/projects/{project_id}/apple/listing",
                     json={**LISTING, "support_url": ""})  # fmt: skip
    url = f"/api/projects/{project_id}/apple/submissions"
    body = {"release_id": release_id, "contact": CONTACT}
    incomplete = await client.post(url, json=body)
    assert incomplete.status_code == 409 and "support_url" in incomplete.text
    await client.put(f"/api/projects/{project_id}/apple/listing", json=LISTING)
    async with server.services.db.session() as session, session.begin():
        session.add(
            AppleApproval(
                id="a2",
                project_id=project_id,
                chat_id="c1",
                user_id=server.services.dev_user_id,
                request_id="r2",
                commit="f" * 40,
                clean=True,
                created_at=time.time() + 1,
            )
        )
    older = await client.post(url, json=body)
    assert older.status_code == 409 and "approved again" in older.text
    bad_contact = await client.post(url, json={**body, "contact": {**CONTACT, "email": "x"}})
    assert bad_contact.status_code == 422
    assert (await client.get(url)).json() == []  # fmt: skip
