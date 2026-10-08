"""Helpers shared by tests: in-memory byte streams and connected multiplexers."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from forge_sandbox.daemon import Daemon
from forge_sandbox.mux import Mux, OpenHandler
from forge_sandbox.protocol import Notify
from forge_web.containers.driver import SandboxLink
from forge_web.sandbox_client import ForwardTarget, SandboxClient

Stream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


async def stream_pair() -> tuple[Stream, Stream]:
    """Two connected byte streams (a socket pair; works on every OS)."""
    left, right = socket.socketpair()
    return await asyncio.open_connection(sock=left), await asyncio.open_connection(sock=right)


@asynccontextmanager
async def mux_pair(
    on_open: OpenHandler | None = None, *, window: int = 1024 * 1024
) -> AsyncIterator[tuple[Mux, Mux]]:
    """A started server-side and daemon-side Mux talking to each other."""
    (sr, sw), (dr, dw) = await stream_pair()
    server = Mux(sr, sw, role="server", window=window)
    daemon = Mux(dr, dw, role="daemon", on_open=on_open, window=window, info={"name": "test"})
    await asyncio.gather(server.start(), daemon.start())
    try:
        yield server, daemon
    finally:
        await server.close()
        await daemon.close()


async def connect(
    daemon: Daemon,
    targets: Mapping[str, ForwardTarget] | None = None,
    notes: asyncio.Queue[Notify] | None = None,
) -> tuple[SandboxClient, asyncio.Task[None]]:
    """A server-side client connected to `daemon` over an in-memory stream."""
    (sr, sw), (dr, dw) = await stream_pair()
    serving = asyncio.create_task(daemon.serve_connection(dr, dw))

    async def close_link() -> None:
        sw.close()

    async def on_notify(message: Notify) -> None:
        if notes is not None:
            notes.put_nowait(message)

    client = SandboxClient(SandboxLink(sr, sw, close_link), targets=targets, on_notify=on_notify)
    await client.start()
    return client, serving


@asynccontextmanager
async def sandbox(
    root: Path,
    targets: Mapping[str, ForwardTarget] | None = None,
    notes: asyncio.Queue[Notify] | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[Daemon, SandboxClient]]:
    daemon = Daemon(root, env={**os.environ, **(env or {})})
    client, serving = await connect(daemon, targets, notes)
    try:
        yield daemon, client
    finally:
        await client.close()
        serving.cancel()
        await daemon.close()


TRIVIAL_SPEC = json.dumps(
    {
        "goal": "answer the user",
        "context": "",
        "requirements": [],
        "constraints": [],
        "acceptance_criteria": ["the user has an answer"],
        "assumptions": [],
        "open_questions": [],
        "size": "trivial",
    }
)


def fake_script(*turns: dict[str, Any], prompts: int = 1) -> dict[str, Any]:
    """A FakeProvider script: `prompts` trivial refinements, then the coder's turns."""
    return {"roles": {"refiner": [{"text": TRIVIAL_SPEC}] * prompts}, "turns": list(turns)}


def call(name: str, **arguments: Any) -> dict[str, Any]:
    """A scripted model turn that calls one tool."""
    return {"text": "", "tool_calls": [{"name": name, "arguments": arguments}]}


class LiveServer:
    """A real Forge Web server on a free port, in a background thread (for HTTP + WebSocket)."""

    def __init__(self, settings: Any, port: int = 0) -> None:
        import threading

        import uvicorn

        from forge_web.app import create_app

        self.app = create_app(settings)
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=port,  # 0: any free port
            log_level="warning",
            loop="asyncio",
            ws="websockets-sansio",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        import time

        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("the test server did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}/api/ws"
        self.services = self.app.state.services
        self.cookie, self.csrf = "", ""
        if self.services.dev_token:
            self.sign_in()
        return self

    def sign_in(self) -> None:
        """Start a session for the development admin (cookies for HTTP and WebSocket)."""
        import httpx

        login = httpx.get(
            f"{self.url}/api/auth/dev-login", params={"token": self.services.dev_token}
        )
        cookies = {c.name: c.value for c in login.cookies.jar}
        self.cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
        self.csrf = cookies.get("forge_csrf", "")

    def headers(self) -> dict[str, str]:
        """What a signed-in client sends: the session cookie and the CSRF value."""
        return {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(30)


def dev_settings(
    data_dir: Path,
    script: dict[str, Any] | None = None,
    *,
    isolation: str = "local",
    image: str | None = None,
) -> Any:
    """Development settings in `data_dir`, with the fake model (and a script, if given)."""
    from forge_web.settings import load_settings

    overrides: dict[str, Any] = {
        "dev.enabled": True,
        "sandbox.isolation": isolation,
        "dev.fake": True,
    }
    if image is not None:
        overrides["sandbox.image"] = image
    if script is not None:
        path = data_dir / "script.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(script))
        overrides["dev.fake_script"] = str(path)
    return load_settings(
        data_dir / "forge-web.toml",
        environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides=overrides,
    )


class Browser:
    """One browser tab: a WebSocket that remembers every message it received."""

    def __init__(self, server: LiveServer) -> None:
        self.server = server
        self.messages: list[dict[str, Any]] = []
        self.ws: Any = None

    async def __aenter__(self) -> "Browser":
        from websockets.asyncio.client import connect

        self.ws = await connect(
            self.server.ws_url, additional_headers={"Cookie": self.server.cookie}
        )
        await self.next("hello")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.ws.close()

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def next(self, message_type: str, timeout: float = 30, **match: Any) -> dict[str, Any]:
        """Read until a message of this type (and fields) arrives; it is returned."""
        async with asyncio.timeout(timeout):
            while True:
                message = json.loads(await self.ws.recv())
                self.messages.append(message)
                if message.get("type") == message_type and all(
                    message.get(k) == v for k, v in match.items()
                ):
                    return message

    async def next_item(self, item_type: str, timeout: float = 30) -> dict[str, Any]:
        """Read until a stored item of this type arrives; the whole message is returned."""
        async with asyncio.timeout(timeout):
            while True:
                message = await self.next("item", timeout)
                if message["item"].get("type") == item_type:
                    return message

    def seqs(self) -> list[int]:
        return [m["seq"] for m in self.messages if m.get("type") == "item"]


class WebClient:
    """An HTTP client that keeps cookies and sends the CSRF value like the web UI does."""

    def __init__(self, server: LiveServer, origin: str | None = None) -> None:
        headers = {"Origin": origin} if origin else {}
        self.client = httpx.AsyncClient(base_url=server.url, headers=headers, timeout=30)

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        csrf = self.client.cookies.get("forge_csrf")
        headers = {"X-CSRF-Token": csrf} if csrf else {}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.request("POST", path, json=body or {})

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path)

    async def __aenter__(self) -> "WebClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.client.aclose()


@dataclass
class Person:
    """A signed-in user and their browser."""

    id: str
    email: str
    web: WebClient


async def person(server: LiveServer, name: str, role: str = "member", **fields: Any) -> Person:
    """A new account with a live session (no password: argon2 would only slow tests down)."""
    import secrets
    import time

    from forge_web.auth.sessions import token_id
    from forge_web.db.models import AuthSession, User

    now, token = time.time(), secrets.token_urlsafe(32)
    values = {"name": name.title(), "role": role, "status": "active", **fields}
    user = User(id=secrets.token_hex(8), email=f"{name}@example.com", created_at=now, **values)
    session_row = AuthSession(id=token_id(token), user_id=user.id, csrf=secrets.token_urlsafe(16),
                              created_at=now, last_seen_at=now, expires_at=now + 3600)  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        session.add(user)
        await session.flush()
        session.add(session_row)
    web = WebClient(server)
    web.client.cookies.set("forge_session", token)
    web.client.cookies.set("forge_csrf", session_row.csrf)
    return Person(user.id, f"{name}@example.com", web)


def free_port() -> int:
    """A TCP port on 127.0.0.1 that is free right now."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def remove_docker_projects(data_dir: Path) -> None:
    """Remove the containers and volumes of the projects in this test server's database."""
    import sqlite3
    import subprocess

    database = data_dir / "forge-web.db"
    if not database.exists():
        return
    with sqlite3.connect(database) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM projects")]
    for project_id in ids:
        found = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=org.forge-web.project={project_id}"],
            capture_output=True, text=True,
        ).stdout.split()  # fmt: skip
        if found:
            subprocess.run(["docker", "rm", "-f", *found], capture_output=True)
    volumes = [f"forge-web-{i}-{kind}" for i in ids for kind in ("workspace", "home")]
    if volumes:
        subprocess.run(["docker", "volume", "rm", "-f", *volumes], capture_output=True)
