"""Settings and administration: profile, password, default model; server settings that admins
change at run time; usage totals that match the database; admin routes refuse members."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from forge_web.auth.passwords import hash_password
from forge_web.db.models import AuditEntry, Project, ProjectMember, UsageRecord
from forge_web.gateway.meter import month_start
from forge_web.settings import load_settings
from support import LiveServer, WebClient, person

PASSWORD = "correct horse battery"


def server_settings(data_dir: Path, **overrides: Any) -> Any:
    values = {"sandbox.isolation": "docker", **overrides}
    return load_settings(
        data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)}, overrides=values
    )


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(server_settings(tmp_path / "data")) as live:
        yield live


def record(user_id: str, kind: str, cost: float, *, at: float | None = None,
           tokens: tuple[int, int] = (100, 10)) -> UsageRecord:  # fmt: skip
    return UsageRecord(
        user_id=user_id, provider="anthropic", model="claude-sonnet-4-5", key_kind=kind,
        input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost,
        created_at=time.time() if at is None else at,
    )  # fmt: skip


async def test_usage_totals_equal_the_database(server: LiveServer) -> None:
    admin = await person(server, "admin", role="admin")
    ada, bob = await person(server, "ada"), await person(server, "bob")
    async with server.services.db.session() as session, session.begin():
        session.add_all([
            record(ada.id, "server", 0.5), record(ada.id, "server", 0.25),
            record(ada.id, "own", 1.0, tokens=(1000, 200)), record(bob.id, "server", 2.0),
            record(ada.id, "server", 9.0, at=month_start() - 3600),  # last month
        ])  # fmt: skip
    overview = (await admin.web.get("/api/admin/usage")).json()
    rows = {row["user_id"]: row for row in overview["users"]}
    assert rows[ada.id]["server_usd"] == pytest.approx(0.75)
    assert rows[ada.id]["own_usd"] == pytest.approx(1.0)
    assert rows[ada.id]["calls"] == 3 and rows[ada.id]["input_tokens"] == 1200
    assert rows[bob.id]["server_usd"] == pytest.approx(2.0)
    async with server.services.db.session() as session:
        in_db = await session.scalar(
            select(func.sum(UsageRecord.cost_usd)).where(UsageRecord.created_at >= month_start())
        )
    assert overview["total_usd"] == pytest.approx(in_db)
    assert sum(r["server_usd"] + r["own_usd"] for r in overview["users"]) == pytest.approx(in_db)
    own = (await ada.web.get("/api/usage")).json()
    assert own["server_keys_usd"] == pytest.approx(rows[ada.id]["server_usd"])
    grant = await admin.web.request("PUT", f"/api/admin/grants/{ada.id}",
                                    json={"allowed": True, "monthly_limit_usd": 5})  # fmt: skip
    assert grant.status_code == 200
    grants = (await admin.web.get("/api/admin/grants")).json()
    assert [(g["user_id"], g["allowed"], g["monthly_limit_usd"]) for g in grants] == [
        (ada.id, True, 5.0)
    ]
    for who in (admin, ada, bob):
        await who.web.client.aclose()


async def test_server_settings_change_at_run_time_and_stay(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with LiveServer(server_settings(data)) as server:
        admin = await person(server, "admin", role="admin")
        current = (await admin.web.get("/api/admin/settings")).json()
        assert current["signup"] == "invite" and current["sandbox_memory"] == "4g"
        async with WebClient(server) as stranger:
            body = {"email": "new@example.com", "password": PASSWORD}
            assert (await stranger.post("/api/auth/signup", body)).status_code == 403
            change = {
                "signup": "open",
                "projects_per_user": 3,
                "sandbox_memory": "2g",
                "sandbox_cpus": 1.5,
                "sandbox_idle_minutes": 10,
                "monthly_limit_usd": 7.5,
            }
            changed = await admin.web.request("PATCH", "/api/admin/settings", json=change)
            assert changed.status_code == 200, changed.text
            assert changed.json()["signup"] == "open"
            assert (await stranger.post("/api/auth/signup", body)).status_code == 201
        settings = server.services.settings
        assert settings.auth.signup == "open" and settings.quotas.projects_per_user == 3
        assert settings.sandbox.memory == "2g" and settings.gateway.monthly_limit_usd == 7.5
        for bad in ({"signup": "anyone"}, {"sandbox_memory": "lots"}, {"sandbox_cpus": 0},
                    {"projects_per_user": -1}, {"unknown": 1}):  # fmt: skip
            refused = await admin.web.request("PATCH", "/api/admin/settings", json=bad)
            assert refused.status_code == 422, bad
        async with server.services.db.session() as session:
            actions = list(await session.scalars(select(AuditEntry.action)))
        assert "settings_changed" in actions
        await admin.web.client.aclose()
    with LiveServer(server_settings(data)) as restarted:  # saved in the database
        assert restarted.services.settings.auth.signup == "open"
        assert restarted.services.settings.sandbox.memory == "2g"


async def test_members_cannot_administer(server: LiveServer) -> None:
    member = await person(server, "member")
    for method, path in (("GET", "/api/admin/settings"), ("PATCH", "/api/admin/settings"),
                         ("GET", "/api/admin/usage"), ("GET", "/api/admin/grants"),
                         ("POST", f"/api/admin/users/{member.id}/totp/reset")):  # fmt: skip
        extra = {"json": {"signup": "open"}} if method == "PATCH" else {}
        response = await member.web.request(method, path, **extra)
        assert response.status_code == 403, (method, path)
    assert server.services.settings.auth.signup == "invite"
    await member.web.client.aclose()


async def test_profile_default_model_and_password(server: LiveServer) -> None:
    ada = await person(server, "ada", password_hash=await hash_password(PASSWORD))
    model = "anthropic/claude-sonnet-4-5"
    patched = await ada.web.request(
        "PATCH", "/api/me", json={"name": "Ada L.", "default_model": model}
    )
    assert patched.status_code == 200, patched.text
    me = (await ada.web.get("/api/me")).json()
    assert me["name"] == "Ada L." and me["default_model"] == model and me["has_password"]
    bad = await ada.web.request("PATCH", "/api/me", json={"default_model": "not a model"})
    assert bad.status_code == 422
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(Project(id="p1", name="P", owner_id=ada.id, created_at=now, updated_at=now))
        await session.flush()
        session.add(ProjectMember(project_id="p1", user_id=ada.id, role="owner"))
    chat = (await ada.web.post("/api/projects/p1/chats", {})).json()
    assert chat["model"] == model  # new chats start with the default model

    new_password = "a new long secret"
    async with WebClient(server) as elsewhere, WebClient(server) as fresh:
        login = {"email": "ada@example.com", "password": PASSWORD}
        assert (await elsewhere.post("/api/auth/login", login)).status_code == 200
        wrong = await ada.web.post("/api/me/password", {"current": "not it", "new": new_password})
        assert wrong.status_code == 403
        weak = await ada.web.post("/api/me/password", {"current": PASSWORD, "new": "short"})
        assert weak.status_code == 422
        changed = await ada.web.post("/api/me/password", {"current": PASSWORD, "new": new_password})
        assert changed.status_code == 200
        assert (await ada.web.get("/api/me")).status_code == 200  # this session stays
        assert (await elsewhere.get("/api/me")).status_code == 401  # other sessions end
        assert (await fresh.post("/api/auth/login", login)).status_code == 401
        renewed = {"email": "ada@example.com", "password": new_password}
        assert (await fresh.post("/api/auth/login", renewed)).status_code == 200
    await ada.web.client.aclose()
