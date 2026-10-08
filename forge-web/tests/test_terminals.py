"""Terminals over WebSockets: typing, resizing, roles, and an output flood that never stalls
the project's chats."""

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from forge_web.db.models import ProjectMember
from forge_web.terminals import ENDED
from support import Browser, LiveServer, dev_settings, person

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="terminals need a POSIX sandbox")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def terminal_url(server: LiveServer, project_id: str, terminal_id: str) -> str:
    return server.ws_url.replace(
        "/api/ws", f"/api/projects/{project_id}/terminals/{terminal_id}/ws"
    )


async def read_until(ws: Any, text: str, timeout: float = 15) -> str:
    seen = b""
    async with asyncio.timeout(timeout):
        while text.encode() not in seen:
            message = await ws.recv()
            seen += message if isinstance(message, bytes) else message.encode()
    return seen.decode("utf-8", "replace")


async def new_terminal(client: httpx.AsyncClient) -> tuple[str, str]:
    pid = (await client.post("/api/projects", json={"name": "Shell"})).json()["id"]
    created = await client.post(f"/api/projects/{pid}/terminals", json={"cols": 80, "rows": 24})
    assert created.status_code == 201, created.text
    return pid, created.json()["id"]


async def test_typing_and_resizing(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid, tid = await new_terminal(client)
    headers = {"Cookie": server.cookie, "Origin": server.url}
    async with connect(terminal_url(server, pid, tid), additional_headers=headers) as ws:
        server.services.runs.last_active[pid] = 0
        await ws.send(b"echo forge-$((40+2))\n")
        assert "forge-42" in await read_until(ws, "forge-42")
        assert server.services.runs.last_active[pid] > 0  # typing keeps the sandbox running
        await ws.send('{"type": "resize", "cols": 100, "rows": 30}')
        await ws.send(b"stty size\n")
        assert "30 100" in await read_until(ws, "30 100")
    listed = (await client.get(f"/api/projects/{pid}/terminals")).json()
    assert [t["id"] for t in listed] == [tid] and listed[0]["cols"] == 100
    assert (await client.delete(f"/api/projects/{pid}/terminals/{tid}")).status_code == 204
    assert (await client.get(f"/api/projects/{pid}/terminals")).json() == []


async def test_a_terminal_that_exits_ends_its_socket(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    pid, tid = await new_terminal(client)
    headers = {"Cookie": server.cookie, "Origin": server.url}
    async with connect(terminal_url(server, pid, tid), additional_headers=headers) as ws:
        await ws.send(b"exit 0\n")
        with pytest.raises(ConnectionClosed):
            await read_until(ws, "never printed")
        assert ws.close_code == ENDED  # the browser shows "ended" instead of reconnecting
    listed = (await client.get(f"/api/projects/{pid}/terminals")).json()
    assert listed[0]["running"] is False and listed[0]["exit_code"] == 0


async def test_an_output_flood_does_not_stall_chats(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    pid, tid = await new_terminal(client)
    headers = {"Cookie": server.cookie, "Origin": server.url}
    async with connect(terminal_url(server, pid, tid), additional_headers=headers) as flood:
        await flood.send(b"yes forge-flood-forge-flood-forge-flood\n")
        await read_until(flood, "forge-flood")  # flooding now; nobody reads it any more
        chat = (await client.post(f"/api/projects/{pid}/chats", json={})).json()
        async with Browser(server) as tab:
            await tab.send({"type": "subscribe", "chat_id": chat["id"], "after_seq": 0})
            await tab.next("subscribed")
            sent = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "hi"})
            assert sent.status_code == 202, sent.text
            turn = await tab.next_item("turn", timeout=45)
            assert turn["item"]["ok"]
    assert (await client.delete(f"/api/projects/{pid}/terminals/{tid}")).status_code == 204


async def test_only_editors_use_terminals(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid, tid = await new_terminal(client)
    viewer = await person(server, "viewer")
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=pid, user_id=viewer.id, role="viewer"))
    assert (await viewer.web.get(f"/api/projects/{pid}/terminals")).status_code == 200
    assert (await viewer.web.post(f"/api/projects/{pid}/terminals", {})).status_code == 403
    outsider = await person(server, "outsider")
    for who in (viewer, outsider):
        cookie = "; ".join(f"{k}={v}" for k, v in who.web.client.cookies.items())
        with pytest.raises(InvalidStatus):
            await connect(terminal_url(server, pid, tid), additional_headers={"Cookie": cookie})
    evil = {"Cookie": server.cookie, "Origin": "https://evil.example"}
    with pytest.raises(InvalidStatus):
        await connect(terminal_url(server, pid, tid), additional_headers=evil)
