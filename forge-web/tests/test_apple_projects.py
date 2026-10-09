"""Apple app projects: they start from Forge's SwiftUI template, their chats build on the
server's Macs with Apple's guidelines checked, and admins set it all up."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from forge_web.apple.template import app_name
from forge_web.db.models import Project
from forge_web.settings import DEFAULT_EGRESS, AppleSettings
from forge_web.startup import chat_options
from support import LiveServer, dev_settings

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def test_an_apple_project_starts_from_the_template(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    body = {"name": "Mein Zähler", "source": "apple", "bundle_id": "de.max.zaehler"}
    created = await client.post("/api/projects", json=body)
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["kind"] == "apple" and project["source"] == "apple"
    workspace = server.services.driver.workspace(project["id"])
    spec = (workspace / "project.yml").read_text()
    assert "name: MeinZaehler\n" in spec and "PRODUCT_BUNDLE_IDENTIFIER: de.max.zaehler\n" in spec
    assert (workspace / "Shared" / "MeinZaehlerApp.swift").is_file()
    icons = workspace / "Shared" / "Assets.xcassets" / "AppIcon.appiconset"
    assert (icons / "icon-1024.png").read_bytes().startswith(PNG)
    assert (workspace / ".git").is_dir()
    async with server.services.db.session() as session:
        row = await session.get(Project, project["id"])
    assert row is not None and row.kind == "apple"
    assert server.services.runs.link_info[project["id"]]["apple"] is True
    listed = (await client.get("/api/projects")).json()
    assert [p["kind"] for p in listed] == ["apple"]


async def test_names_and_bundle_ids_are_checked_first(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    bad = await client.post("/api/projects", json={"name": "Tally", "source": "apple",
                                                   "bundle_id": "not a bundle id"})  # fmt: skip
    assert bad.status_code == 422 and "reverse domain" in bad.json()["detail"]
    assert (await client.get("/api/projects")).json() == []  # nothing was created
    plain = await client.post("/api/projects", json={"name": "2048", "source": "apple"})
    workspace = server.services.driver.workspace(plain.json()["id"])
    assert (
        "PRODUCT_BUNDLE_IDENTIFIER: com.example.app2048\n"
        in (workspace / "project.yml").read_text()
    )


def test_app_names_come_from_project_names() -> None:
    assert app_name("Tally") == "Tally"
    assert app_name("Mein Zähler!") == "MeinZaehler"
    assert app_name("2048") == "App2048"
    assert app_name("Straße & Grüße") == "StrasseGruesse"
    assert app_name("😀") == "App"
    assert len(app_name("x" * 80)) == 30


def test_apple_chats_build_on_macs_with_the_guidelines_checked(tmp_path: Path) -> None:
    settings = dev_settings(tmp_path)
    chat = SimpleNamespace(mode="edits", model="")
    options_for = chat_options(settings)
    link = {"gateway_port": 47101, "apple": True}
    assert "apple_url" not in options_for(chat, link)  # Apple builds are off on this server
    settings.apple.enabled = True
    options = options_for(chat, link)
    assert options["apple_url"] == "http://127.0.0.1:47101" and options["apple_review"] is True
    assert "roles" not in options
    settings.apple.reviewer_model = "openai/gpt-5"
    assert options_for(chat, link)["roles"] == {"apple_reviewer": ["openai/gpt-5"]}
    assert "apple_url" not in options_for(chat, {"gateway_port": 47101, "apple": False})
    assert "developer.apple.com" in DEFAULT_EGRESS  # the reviewer reads Apple's guidelines


async def test_admins_turn_apple_builds_on_and_pick_the_reviewer(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    changes = {"apple_enabled": True, "apple_allowed": "everyone", "apple_minutes_per_month": 90,
               "apple_reviewer_model": "openai/gpt-5"}  # fmt: skip
    saved = await client.patch("/api/admin/settings", json=changes)
    assert saved.status_code == 200, saved.text
    assert {k: saved.json()[k] for k in changes} == changes
    assert server.services.settings.apple.reviewer_model == "openai/gpt-5"
    wrong = await client.patch("/api/admin/settings", json={"apple_reviewer_model": "gpt five"})
    assert wrong.status_code == 422


async def test_the_web_ui_gets_the_device_screenshots(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    created = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    base = f"/api/projects/{created['id']}/apple/screens"
    assert (await client.get(base)).json() == []  # none taken yet
    shots = server.services.driver.workspace(created["id"]) / ".forge" / "out" / "apple"
    shots.mkdir(parents=True)
    (shots / "ipados-dark.png").write_bytes(PNG + b"\0" * 1_200_000)  # more than one part
    (shots / "ios-light.png").write_bytes(PNG + b"\0" * 10)
    (shots / "macos-light.png").write_bytes(b"<svg onload=alert(1)>")  # not a PNG: left out
    (shots / "notes.txt").write_text("hi")
    screens = (await client.get(base)).json()
    assert [(s["platform"], s["dark"]) for s in screens] == [("ios", False), ("ipados", True)]
    assert all(s["url"].startswith("data:image/png;base64,iVBORw0KGgo") for s in screens)


async def test_the_web_ui_knows_who_may_build_apple_apps(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/me")).json()["apple_apps"] is False  # off on this server
    await client.patch("/api/admin/settings", json={"apple_enabled": True})
    assert (await client.get("/api/me")).json()["apple_apps"] is True  # the dev user is an admin
    settings = AppleSettings(enabled=True, allowed="granted")
    assert not settings.allows("member", granted=False) and settings.allows("member", granted=True)
    assert not AppleSettings(enabled=True, allowed="admins").allows("member", granted=True)
    assert AppleSettings(enabled=True, allowed="everyone").allows("member", granted=False)
    assert not AppleSettings(enabled=False, allowed="everyone").allows("admin", granted=True)
