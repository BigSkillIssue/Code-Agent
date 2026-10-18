"""One chat per project (W44): the newest of the old chats stays, every member reads it, editors
write, clearing it leaves an empty one, and its runs use the account of the project's owner."""

import secrets
import time
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from forge_web.chats.one import find_chat, new_chat
from forge_web.db.engine import MIGRATIONS
from forge_web.db.models import Chat, ChatEvent, Project, ProjectMember
from support import LiveServer, Person, person
from test_access import settings


def migrate(url: str, revision: str) -> None:
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    engine.dispose()


def test_the_migration_keeps_each_projects_newest_chat(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    migrate(url, "0037")
    engine = sa.create_engine(url)
    with engine.begin() as db:
        for user in ("owner", "editor"):
            db.execute(sa.text("INSERT INTO users (id, email, created_at) VALUES (:u, :e, 0)"),
                       {"u": user, "e": f"{user}@example.com"})  # fmt: skip
        for project in ("p1", "p2"):
            db.execute(sa.text("INSERT INTO projects (id, name, owner_id, created_at, updated_at) "
                               "VALUES (:p, :p, 'owner', 0, 0)"), {"p": project})  # fmt: skip
        chats = [("old", "p1", "owner", 1.0), ("new", "p1", "editor", 3.0),
                 ("mid", "p1", "owner", 2.0), ("only", "p2", "editor", 1.0)]  # fmt: skip
        for chat_id, project, user, updated in chats:
            db.execute(sa.text("INSERT INTO chats (id, project_id, user_id, shared, created_at, "
                               "updated_at) VALUES (:c, :p, :u, false, 0, :t)"),
                       {"c": chat_id, "p": project, "u": user, "t": updated})  # fmt: skip
            db.execute(sa.text("INSERT INTO chat_events (chat_id, seq, ts, type, data) "
                               "VALUES (:c, 1, 0, 'user', '{}')"), {"c": chat_id})  # fmt: skip
    migrate(url, "head")
    with engine.connect() as db:
        left = db.execute(sa.text("SELECT id, project_id, user_id, shared, started_by FROM chats "
                                  "ORDER BY project_id")).all()  # fmt: skip
        events = db.execute(sa.text("SELECT chat_id FROM chat_events ORDER BY chat_id")).scalars()
        assert [tuple(row) for row in left] == [
            ("new", "p1", "owner", 1, ""),
            ("only", "p2", "owner", 1, ""),
        ]
        assert list(events) == ["new", "only"]
        try:
            db.execute(sa.text("INSERT INTO chats (id, project_id, user_id, created_at, "
                               "updated_at) VALUES ('two', 'p2', 'owner', 0, 0)"))  # fmt: skip
        except sa.exc.IntegrityError:
            pass
        else:
            raise AssertionError("a project took a second chat")
    engine.dispose()


async def shop(server: LiveServer) -> tuple[Person, Person, Person, str]:
    """A project with an owner, an editor and a viewer, and its chat as project creation makes
    it (no sandbox needed for these tests)."""
    owner, editor, viewer = [await person(server, name) for name in ("owner", "editor", "viewer")]
    project = Project(id=secrets.token_hex(8), name="Shop", owner_id=owner.id,
                      created_at=time.time(), updated_at=time.time())  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        session.add(project)
        await session.flush()
        for who, role in ((owner, "owner"), (editor, "editor"), (viewer, "viewer")):
            session.add(ProjectMember(project_id=project.id, user_id=who.id, role=role))
        session.add(await new_chat(session, project))
    return owner, editor, viewer, project.id


async def test_every_member_reads_the_one_chat_and_editors_write(tmp_path: Path) -> None:
    with LiveServer(settings(tmp_path / "data")) as server:
        owner, editor, viewer, project_id = await shop(server)
        seen = []
        for who in (owner, editor, viewer):
            chat = (await who.web.request("GET", f"/api/projects/{project_id}/chat")).json()
            seen.append((chat["id"], chat["can_write"]))
        chat_id = seen[0][0]
        assert seen == [(chat_id, True), (chat_id, True), (chat_id, False)]
        made = await editor.web.request(
            "POST", f"/api/projects/{project_id}/chats", json={"mode": "auto"}
        )
        assert (made.status_code, made.json()["id"], made.json()["mode"]) == (201, chat_id, "auto")
        listed = await viewer.web.request("GET", f"/api/projects/{project_id}/chats")
        assert [c["id"] for c in listed.json()] == [chat_id]
        renamed = await editor.web.request("PATCH", f"/api/chats/{chat_id}", json={"title": "Cart"})
        assert renamed.json()["title"] == "Cart"
        for method, path, body in (("PATCH", "", {"title": "Mine"}), ("DELETE", "", None),
                                   ("POST", "/messages", {"text": "hi"})):  # fmt: skip
            extra = {"json": body} if body is not None else {}
            refused = await viewer.web.request(method, f"/api/chats/{chat_id}{path}", **extra)
            assert refused.status_code == 403, (method, path)
        async with server.services.db.session() as session:
            chat_row = await session.get(Chat, chat_id)
            assert chat_row is not None and chat_row.user_id == owner.id  # the project's account
        for who in (owner, editor, viewer):
            await who.web.client.aclose()


async def test_clearing_leaves_an_empty_chat_and_the_new_owner_pays(tmp_path: Path) -> None:
    with LiveServer(settings(tmp_path / "data")) as server:
        owner, editor, viewer, project_id = await shop(server)
        first = (await editor.web.request("GET", f"/api/projects/{project_id}/chat")).json()
        await editor.web.request("PATCH", f"/api/chats/{first['id']}", json={"mode": "ask"})
        async with server.services.db.session() as session, session.begin():
            session.add(ChatEvent(chat_id=first["id"], seq=1, ts=time.time(), type="user",
                                  data='{"type": "user", "text": "hi"}'))  # fmt: skip
        cleared = await editor.web.request("DELETE", f"/api/chats/{first['id']}")
        assert cleared.status_code == 204
        assert (await editor.web.request("GET", f"/api/chats/{first['id']}")).status_code == 404
        second = (await viewer.web.request("GET", f"/api/projects/{project_id}/chat")).json()
        assert second["id"] != first["id"] and second["mode"] == "ask"
        assert second["title"] == "New chat"
        items = await viewer.web.request("GET", f"/api/chats/{second['id']}/events")
        assert items.json()["items"] == []
        members = f"/api/projects/{project_id}/members"
        promoted = await owner.web.request(
            "PATCH", f"{members}/{editor.id}", json={"role": "owner"}
        )
        assert promoted.status_code == 200
        left = await owner.web.request("DELETE", f"{members}/{owner.id}")
        assert left.status_code == 204
        async with server.services.db.session() as session:
            project = await session.get(Project, project_id)
            chat = await find_chat(session, project_id)
            assert project is not None and chat is not None
            assert project.owner_id == chat.user_id == editor.id  # runs go on the new owner
        for who in (owner, editor, viewer):
            await who.web.client.aclose()


async def test_a_project_from_before_gets_its_chat_on_first_use(tmp_path: Path) -> None:
    with LiveServer(settings(tmp_path / "data")) as server:
        owner = await person(server, "owner")
        project_id, now = secrets.token_hex(8), time.time()
        async with server.services.db.session() as session, session.begin():
            session.add(Project(id=project_id, name="Old", owner_id=owner.id, created_at=now,
                                updated_at=now))  # fmt: skip
            await session.flush()
            session.add(ProjectMember(project_id=project_id, user_id=owner.id, role="owner"))
        first = await owner.web.request("GET", f"/api/projects/{project_id}/chat")
        again = await owner.web.request("GET", f"/api/projects/{project_id}/chat")
        assert first.status_code == 200 and first.json()["id"] == again.json()["id"]
        await owner.web.client.aclose()
