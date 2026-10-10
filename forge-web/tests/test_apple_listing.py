"""The App Store listing in Forge Web (W22c): Forge's draft from the project, finished and saved
by the user, checked with Forge's own model; and its review on the approval page."""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.apple_listing import LISTING
from sqlalchemy import select

from forge_web.db.models import AuditEntry
from support import LiveServer, dev_settings

DRAFT: dict[str, Any] = {
    "locale": "en-US", "name": "Tally", "subtitle": "Count anything",
    "description": "Tap to count. That is all.", "keywords": "counter,tally,count",
    "primary_category": "UTILITIES", "copyright": "2026 Ada",
    "notes": ["Check that the screenshots show the counter."],
}  # fmt: skip


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.apple.enabled, settings.apple.allowed = True, "everyone"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def apple_project(server: LiveServer, client: httpx.AsyncClient) -> tuple[str, Path]:
    project = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    return str(project["id"]), server.services.driver.workspace(project["id"])


def write_draft(workspace: Path, draft: dict[str, Any]) -> None:
    """What Forge leaves in the project after the approval (S61)."""
    path = workspace / LISTING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft))


async def test_forges_draft_is_shown_finished_and_saved(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id, workspace = await apple_project(server, client)
    url = f"/api/projects/{project_id}/apple/listing"
    assert (await client.get(url)).json()["source"] == "none"  # not drafted yet
    write_draft(workspace, DRAFT)
    shown = (await client.get(url)).json()
    assert shown["source"] == "draft" and shown["listing"]["name"] == "Tally"
    assert shown["missing"] == ["support_url", "privacy_policy_url"]  # always the user's
    finished = {**shown["listing"], "support_url": "https://example.com/help",
                "privacy_policy_url": "https://example.com/privacy"}  # fmt: skip
    saved = await client.put(url, json=finished)
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "saved" and saved.json()["missing"] == []
    write_draft(workspace, {**DRAFT, "name": "Changed later"})  # the sandbox's file may change
    assert (await client.get(url)).json()["listing"]["name"] == "Tally"  # what was saved counts
    redraft = (await client.get(f"{url}/draft")).json()
    assert redraft["source"] == "draft" and redraft["listing"]["name"] == "Changed later"
    async with server.services.db.session() as session:
        actions = [a.action for a in await session.scalars(select(AuditEntry))]
    assert "apple.listing_saved" in actions


async def test_apples_limits_hold_for_every_save(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id, workspace = await apple_project(server, client)
    url = f"/api/projects/{project_id}/apple/listing"
    write_draft(workspace, DRAFT)
    listing = (await client.get(url)).json()["listing"]
    for changes, field in (({"name": "x" * 31}, "name"), ({"keywords": "ä" * 51}, "keywords"),
                           ({"support_url": "http://example.com"}, "support_url"),
                           ({"primary_category": "TOYS"}, "primary_category"),
                           ({"promotional_text": "y" * 171}, "promotional_text")):  # fmt: skip
        refused = await client.put(url, json={**listing, **changes})
        assert refused.status_code == 422 and field in refused.json()["detail"], refused.text
    write_draft(workspace, {**DRAFT, "subtitle": "z" * 40})  # a draft over the limits
    broken = await client.get(f"{url}/draft")
    assert broken.status_code == 422 and "Apple's limits" in broken.text
