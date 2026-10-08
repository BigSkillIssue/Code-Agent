"""Who may call what: every API route against strangers, outsiders, viewers and non-admins;
project members, administration and a user's own sessions."""

import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from forge_web.app import create_app
from forge_web.db.models import AuditEntry, AuthSession, Chat, Project, ProjectMember, User
from forge_web.settings import load_settings
from support import LiveServer, Person, WebClient, person

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Anyone may call these (signing in and the like); test_auth covers them.
PUBLIC = {
    "GET /api/health", "GET /api/auth/config", "GET /api/auth/dev-login",
    "GET /api/auth/invite/{token}", "POST /api/auth/setup", "POST /api/auth/login",
    "POST /api/auth/signup", "POST /api/auth/reset", "POST /api/auth/forgot",
    "POST /api/auth/verify", "GET /api/auth/oauth/{name}/start",
    "GET /api/auth/oauth/{name}/callback",
}  # fmt: skip
# These act on the caller's own things: any signed-in user may call them.
OWN = {
    "GET /api/me", "POST /api/auth/logout", "GET /api/auth/sessions",
    "DELETE /api/auth/sessions/{session_id}", "GET /api/projects", "POST /api/projects",
    "GET /api/keys", "POST /api/keys", "DELETE /api/keys/{key_id}", "GET /api/providers",
    "GET /api/usage", "GET /api/auth/identities", "DELETE /api/auth/identities/{provider}",
    "GET /api/git/credentials", "POST /api/git/credentials",
    "DELETE /api/git/credentials/{credential_id}", "GET /api/models",
}  # fmt: skip
# Project files and git live in the project's sandbox, which the access tests do not start:
# once access is granted, these routes answer 503 here.
SANDBOX_PATHS = (
    "/api/projects/{project_id}/files", "/api/projects/{project_id}/git/",
    "/api/projects/{project_id}/usage", "/api/projects/{project_id}/terminals",
)  # fmt: skip
# A valid body for every request model, so a refusal is about access, not validation.
BODIES: dict[str, dict[str, Any]] = {
    "UserPatch": {"status": "disabled"},
    "InviteIn": {"email": "new@example.com"},
    "ProjectIn": {"name": "Mine"},
    "ProjectPatch": {"name": "Taken over"},
    "MemberIn": {"email": "outsider@example.com", "role": "owner"},
    "RoleIn": {"role": "viewer"},
    "ChatIn": {"title": "Sneaky"},
    "ChatPatch": {"title": "Renamed", "shared": True},
    "MessageIn": {"text": "rm -rf /workspace"},
    "AnswerIn": {"request_id": "r1", "answer": {"approved": True}},
    "KeyIn": {"provider": "anthropic", "key": "sk-ant-not-a-real-key"},
    "GrantIn": {"allowed": True, "monthly_limit_usd": 1000},
    "CredentialIn": {"host": "github.com", "token": "ghp_not_a_real_token"},
    "TextFileIn": {"path": "src/app.py", "text": "print('owned')\n"},
    "PathIn": {"path": "new-folder"},
    "RenameIn": {"src": "README.md", "dst": "GONE.md"},
    "PathsIn": {"paths": ["."]},
    "CommitIn": {"message": "sneaky commit"},
    "SwitchIn": {"branch": "evil", "create": True},
    "RemoteIn": {"url": "https://example.com/evil.git"},
    "SyncIn": {"branch": "main"},
    "TerminalIn": {"cols": 80, "rows": 24},
}


@dataclass
class World:
    """A server with an admin, a project (owner + viewer), a shared chat, and an outsider."""

    server: LiveServer
    admin: Person
    owner: Person
    viewer: Person
    outsider: Person
    project_id: str
    chat_id: str


def settings(data_dir: Path) -> Any:
    return load_settings(
        data_dir / "forge-web.toml",
        environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides={"sandbox.isolation": "docker"},
    )


async def project_with_chat(server: LiveServer, owner: Person, viewer: Person) -> tuple[str, str]:
    """A project (no sandbox needed for these tests) with one shared chat of the owner."""
    now, project_id, chat_id = time.time(), secrets.token_hex(8), secrets.token_hex(16)
    async with server.services.db.session() as session, session.begin():
        session.add(Project(id=project_id, name="Secret", owner_id=owner.id, created_at=now,
                            updated_at=now))  # fmt: skip
        await session.flush()
        session.add(ProjectMember(project_id=project_id, user_id=owner.id, role="owner"))
        session.add(ProjectMember(project_id=project_id, user_id=viewer.id, role="viewer"))
        session.add(Chat(id=chat_id, project_id=project_id, user_id=owner.id, shared=True,
                         created_at=now, updated_at=now))  # fmt: skip
    return project_id, chat_id


@asynccontextmanager
async def new_world(data_dir: Path) -> AsyncIterator[World]:
    with LiveServer(settings(data_dir)) as server:
        admin = await person(server, "admin", role="admin")
        owner, viewer, outsider = [await person(server, n) for n in ("owner", "viewer", "outsider")]
        project_id, chat_id = await project_with_chat(server, owner, viewer)
        try:
            yield World(server, admin, owner, viewer, outsider, project_id, chat_id)
        finally:
            for p in (admin, owner, viewer, outsider):
                await p.web.client.aclose()


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    """A world of its own, for tests that change it."""
    async with new_world(tmp_path / "data") as fresh:
        yield fresh


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[World]:
    """One world for the tests that are only refused (and so change nothing)."""
    async with new_world(tmp_path_factory.mktemp("access") / "data") as world:
        yield world


on_shared_loop = pytest.mark.asyncio(loop_scope="module")


def api_routes(app: Any) -> list[tuple[str, str, str | None]]:
    """(method, path, request model) of every API route."""
    routes = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            ref = content.get("application/json", {}).get("schema", {}).get("$ref", "")
            routes.append((method.upper(), path, ref.rsplit("/", 1)[-1] or None))
    return routes


def rule(method: str, path: str) -> str:
    """Which access rule a route follows."""
    key = f"{method} {path}"
    if key in PUBLIC:
        return "public"
    if key in OWN:
        return "own"
    if path.startswith("/api/admin/"):
        return "admin"
    if "{project_id}" in path or "{chat_id}" in path:
        return "project"
    raise AssertionError(f"{key} has no access rule: add it to PUBLIC or OWN, or to this test")


def filled(path: str, world: World) -> str:
    """The path with real ids of the world's things."""
    values = {
        "project_id": world.project_id, "chat_id": world.chat_id, "member_id": world.owner.id,
        "user_id": world.owner.id, "token": "x" * 20, "invite_id": "0" * 16,
        "session_id": "0" * 16, "key_id": "0" * 16, "name": "google", "provider": "google",
        "credential_id": "0" * 16, "terminal_id": "t0123abcd",
    }  # fmt: skip
    return re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], path)


# Query parameters some routes need; routes without them ignore them.
QUERY = {"path": "README.md"}


async def call(who: Person | WebClient, method: str, path: str, model: str | None) -> int:
    web = who.web if isinstance(who, Person) else who
    body = BODIES[model] if model is not None else ({} if method in UNSAFE else None)
    extra: dict[str, Any] = {"json": body} if body is not None else {}
    response = await web.request(method, path, params=QUERY, **extra)
    return int(response.status_code)


def test_every_route_has_a_rule_and_a_sample_body(tmp_path: Path) -> None:
    routes = api_routes(create_app(settings(tmp_path / "data")))
    assert len(routes) > 40
    for method, path, model in routes:
        if rule(method, path) != "public" and model is not None:
            assert model in BODIES, f"add a sample {model} body for {method} {path}"
    assert "/openapi.json" not in {path for _m, path, _b in routes}


@on_shared_loop
async def test_strangers_must_sign_in_everywhere(shared: World) -> None:
    world = shared
    async with WebClient(world.server) as stranger:
        for method, path, model in api_routes(world.server.app):
            if rule(method, path) != "public":
                status = await call(stranger, method, filled(path, world), model)
                assert status == 401, f"{method} {path} answered {status}"


@on_shared_loop
async def test_outsiders_cannot_see_projects_or_administer(shared: World) -> None:
    world = shared
    for method, path, model in api_routes(world.server.app):
        kind = rule(method, path)
        if kind in ("project", "admin"):
            status = await call(world.outsider, method, filled(path, world), model)
            expected = 404 if kind == "project" else 403
            assert status == expected, f"{method} {path} answered {status}"
    async with world.server.services.db.session() as session:
        members = list(await session.scalars(select(ProjectMember.user_id)))
    assert world.outsider.id not in members  # adding oneself did not work


@on_shared_loop
async def test_viewers_only_read(shared: World) -> None:
    world = shared
    for method, path, model in api_routes(world.server.app):
        kind = rule(method, path)
        if kind not in ("admin", "project"):
            continue
        status = await call(world.viewer, method, filled(path, world), model)
        if kind == "admin":
            assert status == 403, f"{method} {path} answered {status}"
        elif kind == "project" and method in UNSAFE:
            assert status in (403, 404), f"{method} {path} answered {status}"
        elif kind == "project":
            assert status == granted(method, path), f"{method} {path} answered {status}"


@on_shared_loop
async def test_the_owner_reads_every_project_route(shared: World) -> None:
    world = shared
    for method, path, model in api_routes(world.server.app):
        if rule(method, path) == "project" and method == "GET":
            status = await call(world.owner, method, filled(path, world), model)
            assert status == granted(method, path), f"{method} {path} answered {status}"


def granted(method: str, path: str) -> int:
    """What a permitted read answers in these tests."""
    return 503 if path.startswith(SANDBOX_PATHS) else 200


@on_shared_loop
async def test_outsiders_cannot_follow_a_chat(shared: World) -> None:
    world = shared
    from websockets.asyncio.client import connect

    cookie = "; ".join(f"{k}={v}" for k, v in world.outsider.web.client.cookies.items())
    async with connect(world.server.ws_url, additional_headers={"Cookie": cookie}) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        await ws.send(json.dumps({"type": "subscribe", "chat_id": world.chat_id, "after_seq": 0}))
        reply = json.loads(await ws.recv())
    assert reply["type"] == "error" and reply["chat_id"] == world.chat_id


async def test_members_are_managed_by_owners(world: World) -> None:
    base = f"/api/projects/{world.project_id}/members"
    owner = world.owner.web
    added = await owner.post(base, {"email": "OUTSIDER@example.com", "role": "editor"})
    assert added.status_code == 201 and added.json()["role"] == "editor"
    assert (await owner.post(base, {"email": world.outsider.email})).status_code == 409
    assert (await owner.post(base, {"email": "nobody@example.com"})).status_code == 404
    names = [p["name"] for p in (await world.outsider.web.get("/api/projects")).json()]
    assert names == ["Secret"]
    roles = {m["email"]: m["role"] for m in (await world.viewer.web.get(base)).json()}
    assert roles == {"owner@example.com": "owner", "viewer@example.com": "viewer",
                     "outsider@example.com": "editor"}  # fmt: skip

    demote_self = await owner.request("PATCH", f"{base}/{world.owner.id}", json={"role": "editor"})
    assert demote_self.status_code == 409  # the last owner
    assert (await owner.request("DELETE", f"{base}/{world.owner.id}")).status_code == 409
    promote = await owner.request("PATCH", f"{base}/{world.outsider.id}", json={"role": "owner"})
    assert promote.status_code == 200
    assert (await owner.request("DELETE", f"{base}/{world.owner.id}")).status_code == 204
    assert (await owner.get(f"/api/projects/{world.project_id}")).status_code == 404  # left
    leave = await world.viewer.web.request("DELETE", f"{base}/{world.viewer.id}")
    assert leave.status_code == 204
    async with world.server.services.db.session() as session:
        actions = list(await session.scalars(select(AuditEntry.action)))
    assert {"member_added", "member_role", "member_removed"} <= set(actions)


async def test_admins_manage_accounts(world: World) -> None:
    admin = world.admin.web
    emails = [u["email"] for u in (await admin.get("/api/admin/users")).json()]
    assert "owner@example.com" in emails
    last = await admin.request(
        "PATCH", f"/api/admin/users/{world.admin.id}", json={"role": "member"}
    )
    assert last.status_code == 409  # the server keeps one admin
    disable = {"status": "disabled"}
    off = await admin.request("PATCH", f"/api/admin/users/{world.viewer.id}", json=disable)
    assert off.status_code == 200 and off.json()["status"] == "disabled"
    assert (await world.viewer.web.get("/api/me")).status_code == 401  # signed out everywhere
    async with world.server.services.db.session() as session, session.begin():
        user = await session.get(User, world.outsider.id)
        assert user is not None
        user.status = "pending"
    approved = await admin.post(f"/api/admin/users/{world.outsider.id}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    assert (await admin.post(f"/api/admin/users/{world.outsider.id}/approve")).status_code == 404

    link = (await admin.post(f"/api/admin/users/{world.owner.id}/reset-link")).json()["link"]
    assert "/reset#token=" in link
    async with WebClient(world.server) as fresh:
        reset = {"token": link.split("#token=")[1], "password": "a fresh passphrase here"}
        assert (await fresh.post("/api/auth/reset", reset)).status_code == 200
        assert (await fresh.get("/api/me")).json()["id"] == world.owner.id
    actions = [e["action"] for e in (await admin.get("/api/admin/audit")).json()]
    assert {"user_changed", "user_approved", "reset_link"} <= set(actions)


async def test_admins_invite_and_withdraw(world: World) -> None:
    admin = world.admin.web
    created = await admin.post("/api/admin/invites", {"email": "Guest@Example.com"})
    assert created.status_code == 201 and "/signup#token=" in created.json()["link"]
    token = created.json()["link"].split("#token=")[1]
    listed = (await admin.get("/api/admin/invites")).json()
    assert [i["email"] for i in listed] == ["guest@example.com"]
    assert token not in json.dumps(listed)  # only a short id of the hash
    assert (await admin.request("DELETE", "/api/admin/invites/abc")).status_code == 404
    gone = await admin.request("DELETE", f"/api/admin/invites/{listed[0]['id']}")
    assert gone.status_code == 204
    assert (await admin.get("/api/admin/invites")).json() == []
    async with WebClient(world.server) as guest:
        info = await guest.get(f"/api/auth/invite/{token}")
        assert info.status_code == 404


async def test_people_see_and_end_their_own_sessions(world: World) -> None:
    second = await person(world.server, "owner2")
    async with world.server.services.db.session() as session, session.begin():
        row = await session.scalar(select(AuthSession).where(AuthSession.user_id == second.id))
        assert row is not None
        row.user_id = world.owner.id  # the owner, signed in on a second browser
    sessions = (await world.owner.web.get("/api/auth/sessions")).json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["current"])
    meddle = await world.outsider.web.request("DELETE", f"/api/auth/sessions/{other['id']}")
    assert meddle.status_code == 204
    assert (await second.web.get("/api/me")).status_code == 200  # not theirs to end
    ended = await world.owner.web.request("DELETE", f"/api/auth/sessions/{other['id']}")
    assert ended.status_code == 204
    assert (await second.web.get("/api/me")).status_code == 401
    assert (await world.owner.web.get("/api/me")).status_code == 200
    await second.web.client.aclose()
