"""Terminals in a project's sandbox: list, create and close them over HTTP, and use one over its
own WebSocket (`/api/projects/{id}/terminals/{tid}/ws`): binary messages are keystrokes (in) and
output (out), a text message `{"type": "resize", "cols", "rows"}` changes the size.

Only editors may create or use terminals (viewers may list them). Each terminal is a `pty`
channel on the project's sandbox connection with its own flow control, so a terminal that
floods output slows down only itself, never the project's chats.

Once open, the socket closes with ENDED when the terminal is gone (its program exited, someone
closed it, or the sandbox stopped) and with RESTARTING when the server shuts down, so the
browser knows whether reconnecting can help.
"""

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from forge_sandbox.mux import Channel, ChannelClosed, OpenFailed, OpenRefused
from forge_web import live_access
from forge_web.access import require_project
from forge_web.auth.sessions import CurrentUser, session_token, websocket_user
from forge_web.containers.driver import SandboxError
from forge_web.files_api import allowed
from forge_web.sandbox_calls import sandbox_call
from forge_web.services import Services, services_of

TERMINAL_ID = re.compile(r"^t[0-9a-f]{8}$")
MAX_INPUT = 64 * 1024  # bytes of keystrokes per message
ENDED = 4404
RESTARTING = 1012  # the standard "service restart" close code


class TerminalIn(BaseModel):
    """The size of a new terminal."""

    cols: int = Field(default=80, ge=10, le=500)
    rows: int = Field(default=24, ge=4, le=200)


class Resize(BaseModel):
    """A new terminal size, from the browser."""

    type: str = Field(pattern="^resize$")
    cols: int = Field(ge=10, le=500)
    rows: int = Field(ge=4, le=200)


def check_terminal_id(terminal_id: str) -> str:
    """A terminal id as the sandbox makes them; 404 otherwise."""
    if not TERMINAL_ID.match(terminal_id):
        raise HTTPException(404, "no such terminal")
    return terminal_id


def terminals_router() -> APIRouter:
    """`/api/projects/{id}/terminals` and each terminal's WebSocket."""
    router = APIRouter(prefix="/api/projects/{project_id}/terminals")

    @router.get("")
    async def terminals(project_id: str, request: Request, user: CurrentUser) -> list[Any]:
        services = await allowed(request, user, project_id)
        found = await sandbox_call(services, project_id, "pty.list")
        return found if isinstance(found, list) else []

    @router.post("", status_code=201)
    async def create(project_id: str, body: TerminalIn, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        params = {"cols": body.cols, "rows": body.rows}
        return await sandbox_call(services, project_id, "pty.create", params)

    @router.delete("/{terminal_id}", status_code=204)
    async def close(project_id: str, terminal_id: str, request: Request, user: CurrentUser) -> None:
        services = await allowed(request, user, project_id, "editor")
        params = {"id": check_terminal_id(terminal_id)}
        await sandbox_call(services, project_id, "pty.close", params)

    @router.websocket("/{terminal_id}/ws")
    async def socket(websocket: WebSocket, project_id: str, terminal_id: str) -> None:
        channel = await open_terminal(websocket, project_id, terminal_id)
        if channel is not None:
            await relay(websocket, channel, project_id)

    return router


async def open_terminal(websocket: WebSocket, project_id: str, terminal_id: str) -> Channel | None:
    """The terminal's channel once the user may use it (the socket is closed otherwise)."""
    services = services_of(websocket)
    user = await websocket_user(websocket)
    if user is None or not TERMINAL_ID.match(terminal_id):
        await websocket.close(code=4401 if user is None else 4404)
        return None
    async with services.db.session() as session:
        try:
            await require_project(session, user, project_id, "editor")
        except HTTPException as err:
            await websocket.close(code=4403 if err.status_code == 403 else 4404)
            return None
    channel = await attach(services, project_id, terminal_id)
    if channel is None:
        await websocket.close(code=4404)
        return None
    await websocket.accept()
    return channel


async def attach(services: Services, project_id: str, terminal_id: str) -> Channel | None:
    """A `pty` channel to the terminal, or None if it does not exist."""
    try:
        client = await services.runs.link(project_id)
        return await client.open("pty", {"id": terminal_id})
    except (OpenFailed, OpenRefused, ChannelClosed, SandboxError, OSError, TimeoutError):
        return None


async def relay(websocket: WebSocket, channel: Channel, project_id: str) -> None:
    """Keystrokes and sizes to the terminal, its output to the browser, until either side ends."""
    runs = services_of(websocket).runs
    output = asyncio.create_task(to_browser(channel, websocket, lambda: runs.closing))
    watcher = asyncio.create_task(watch_access(websocket, project_id))
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            runs.touch(project_id)
            if message.get("bytes"):
                await channel.send(message["bytes"][:MAX_INPUT])
            elif message.get("text"):
                with contextlib.suppress(ValueError, ValidationError):
                    size = Resize.model_validate(json.loads(message["text"]))
                    await channel.send_message(size.model_dump())
    except (WebSocketDisconnect, ChannelClosed, RuntimeError):
        pass
    finally:
        output.cancel()
        watcher.cancel()
        with contextlib.suppress(Exception):
            await channel.close()


async def watch_access(websocket: WebSocket, project_id: str) -> None:
    """Close the terminal once its user may no longer use it (signed out, removed, demoted)."""
    services, token = services_of(websocket), session_token(websocket.cookies)
    while True:
        await asyncio.sleep(live_access.RECHECK_SECONDS)
        if not await live_access.may_use_project(services, token, project_id, "editor"):
            with contextlib.suppress(Exception):
                await websocket.close(code=4403)
            return


async def to_browser(channel: Channel, websocket: WebSocket, closing: Callable[[], bool]) -> None:
    """The terminal's output as binary messages; closes the socket when the terminal ends."""
    try:
        while True:
            item = await channel.receive()
            if isinstance(item, bytes):
                await websocket.send_bytes(item)
    except (ChannelClosed, WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close(code=RESTARTING if closing() else ENDED)
