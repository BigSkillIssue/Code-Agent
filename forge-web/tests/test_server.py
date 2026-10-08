"""The single-user server: sign-in, projects, chats, the event log and the WebSocket."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.db.engine import Database
from support import Browser, LiveServer, call, dev_settings, fake_script


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


def scripted(tmp_path: Path, script: dict[str, Any]) -> LiveServer:
    return LiveServer(dev_settings(tmp_path / "data", script))


def api(server: LiveServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, headers={"Cookie": server.cookie}, timeout=60)


async def new_chat(client: httpx.AsyncClient, **chat: Any) -> tuple[str, str]:
    project = (await client.post("/api/projects", json={"name": "Demo"})).json()
    created = await client.post(f"/api/projects/{project['id']}/chats", json=chat)
    assert created.status_code == 201, created.text
    return project["id"], created.json()["id"]


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await db.migrate()
    await db.migrate()
    async with db.engine.connect() as connection:
        mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
        tables = (
            (await connection.exec_driver_sql("SELECT name FROM sqlite_master")).scalars().all()
        )
    assert mode == "wal"
    assert {"users", "projects", "project_members", "chats", "chat_events"} <= set(tables)
    await db.close()


async def test_sign_in_is_required(server: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=server.url) as anonymous:
        assert (await anonymous.get("/api/projects")).status_code == 401
        bad = await anonymous.get("/api/auth/dev-login", params={"token": "wrong"})
        assert bad.status_code == 403
        good = await anonymous.get(
            "/api/auth/dev-login", params={"token": server.services.dev_token}
        )
        assert good.status_code == 303 and "forge_dev" in good.headers["set-cookie"]
        assert "httponly" in good.headers["set-cookie"].lower()
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    with pytest.raises(InvalidStatus) as rejected:  # refused during the handshake
        await connect(server.ws_url)
    assert rejected.value.response.status_code == 403


async def test_project_lifecycle(server: LiveServer) -> None:
    async with api(server) as client:
        created = await client.post("/api/projects", json={"name": "  My app "})
        assert created.status_code == 201
        project = created.json()
        assert project["name"] == "My app" and project["role"] == "owner"
        workspace = server.services.driver.workspace(project["id"])
        assert (workspace / ".git").is_dir()
        assert [p["id"] for p in (await client.get("/api/projects")).json()] == [project["id"]]
        renamed = await client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"})
        assert renamed.json()["name"] == "Renamed"
        assert (await client.get("/api/projects/nope")).status_code == 404
        assert (await client.delete(f"/api/projects/{project['id']}")).status_code == 204
        assert not workspace.exists()
        assert (await client.get("/api/projects")).json() == []


async def test_a_chat_turn_streams_and_is_stored(server: LiveServer) -> None:
    async with api(server) as client, Browser(server) as tab:
        _, chat_id = await new_chat(client)
        await tab.send({"type": "subscribe", "chat_id": chat_id, "after_seq": 0})
        await tab.next("subscribed", chat_id=chat_id)
        sent = await client.post(f"/api/chats/{chat_id}/messages", json={"text": "Say hello"})
        assert sent.status_code == 202, sent.text
        turn = await tab.next_item("turn")
        assert turn["item"]["ok"] and "fake model" in turn["item"]["summary"]
        assert any(m["type"] == "live" for m in tab.messages)  # streamed text
        seqs = tab.seqs()
        assert seqs == list(range(1, len(seqs) + 1))
        stored = (await client.get(f"/api/chats/{chat_id}/events")).json()
        assert [entry["seq"] for entry in stored["items"]][: len(seqs)] == seqs
        await tab.next("chat_state", chat_id=chat_id, state="idle")
        chat = (await client.get(f"/api/chats/{chat_id}")).json()
        assert chat["state"] == "idle" and chat["title"] == "Say hello"


async def test_replay_after_any_number_has_no_gaps_or_repeats(server: LiveServer) -> None:
    async with api(server) as client:
        _, chat_id = await new_chat(client)
        async with Browser(server) as first:
            await first.send({"type": "subscribe", "chat_id": chat_id, "after_seq": 0})
            await first.next("subscribed")
            await client.post(f"/api/chats/{chat_id}/messages", json={"text": "one"})
            await first.next_item("turn")
            await first.next("chat_state", state="idle")
        last = (await client.get(f"/api/chats/{chat_id}/events")).json()["last_seq"]
        middle = last // 2
        async with Browser(server) as second:
            await second.send({"type": "subscribe", "chat_id": chat_id, "after_seq": middle})
            subscribed = await second.next("subscribed")
            assert second.seqs() == list(range(middle + 1, last + 1))
            assert subscribed["last_seq"] == last
            await client.post(f"/api/chats/{chat_id}/messages", json={"text": "two"})
            await second.next_item("turn")
            seqs = second.seqs()
            assert seqs == list(range(middle + 1, seqs[-1] + 1))


async def test_two_tabs_answer_and_the_first_answer_wins(tmp_path: Path) -> None:
    script = fake_script(call("write_file", path="a.txt", content="A\n"), {"text": "Done."})
    with scripted(tmp_path, script) as server:
        async with api(server) as client, Browser(server) as tab1, Browser(server) as tab2:
            project_id, chat_id = await new_chat(client, mode="ask")
            for tab in (tab1, tab2):
                await tab.send({"type": "subscribe", "chat_id": chat_id, "after_seq": 0})
                await tab.next("subscribed")
            await client.post(f"/api/chats/{chat_id}/messages", json={"text": "Write a.txt"})
            request = (await tab1.next_item("request"))["item"]
            assert (
                request["kind"] == "approval" and request["payload"]["call"]["name"] == "write_file"
            )
            await tab2.next_item("request")
            await tab1.next("chat_state", state="waiting")
            body = {"request_id": request["id"], "answer": {"allow": True}}
            answers = await asyncio.gather(
                client.post(f"/api/chats/{chat_id}/answer", json=body),
                client.post(f"/api/chats/{chat_id}/answer", json=body),
            )
            assert sorted(a.json()["accepted"] for a in answers) == [False, True]
            for tab in (tab1, tab2):
                resolved = await tab.next_item("request_resolved")
                assert resolved["item"]["id"] == request["id"]
            await tab1.next_item("turn")
            assert (server.services.driver.workspace(project_id) / "a.txt").read_text() == "A\n"


async def test_a_tab_that_reconnects_mid_turn_misses_nothing(tmp_path: Path) -> None:
    script = fake_script(
        {"text": "step one", "delay_s": 0.5, "tool_calls": [{"name": "list_dir", "arguments": {}}]},
        {"text": "all done", "delay_s": 0.5},
    )
    with scripted(tmp_path, script) as server:
        async with api(server) as client:
            _, chat_id = await new_chat(client)
            async with Browser(server) as tab:
                await tab.send({"type": "subscribe", "chat_id": chat_id, "after_seq": 0})
                await tab.next("subscribed")
                await client.post(f"/api/chats/{chat_id}/messages", json={"text": "go"})
                await tab.next_item("user")
                seen = tab.seqs()
            async with Browser(server) as again:
                await again.send({"type": "subscribe", "chat_id": chat_id, "after_seq": seen[-1]})
                await again.next_item("turn")
                assert seen + again.seqs() == list(range(1, again.seqs()[-1] + 1))


async def test_chat_settings_and_deletion(server: LiveServer) -> None:
    async with api(server) as client:
        project_id, chat_id = await new_chat(client, title="Plan", mode="auto")
        patched = await client.patch(
            f"/api/chats/{chat_id}", json={"mode": "ask", "title": "Renamed"}
        )
        assert patched.json()["mode"] == "ask" and patched.json()["title"] == "Renamed"
        bad = await client.patch(f"/api/chats/{chat_id}", json={"mode": "yolo"})
        assert bad.status_code == 422
        listed = (await client.get(f"/api/projects/{project_id}/chats")).json()
        assert [c["id"] for c in listed] == [chat_id] and listed[0]["mine"]
        assert (await client.delete(f"/api/chats/{chat_id}")).status_code == 204
        assert (await client.get(f"/api/chats/{chat_id}")).status_code == 404
