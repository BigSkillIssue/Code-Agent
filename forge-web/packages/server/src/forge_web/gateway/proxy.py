"""The gateway's HTTP side: checks the run token, swaps in a real key, forwards, meters.

It listens only on a private socket of the server (a unix socket, or loopback on Windows);
sandboxes reach it through their daemon's `forward` channel, never over a network.
"""

import asyncio
import contextlib
import json
import logging
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from forge_web.db.engine import Database
from forge_web.db.models import Chat, UsageRecord, User
from forge_web.gateway.keys import ChosenKey, choose_key
from forge_web.gateway.meter import CHARS_PER_TOKEN, Ledger, UsageSniffer, cost
from forge_web.gateway.tokens import parse_token, run_token
from forge_web.gateway.upstreams import (
    FORWARD_HEADERS,
    Upstream,
    allowed_path,
    auth_headers,
    error_body,
    incoming_token,
    model_of,
    prepare_body,
    upstream,
)
from forge_web.settings import GatewaySettings
from forge_web.vault import Vault

log = logging.getLogger(__name__)
MAX_BODY = 8 * 1024 * 1024
MAX_SOCKET_PATH = 100
ChatIsWorking = Callable[[str], bool]


class Refusal(Exception):
    """A request the gateway answers itself with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Gateway:
    """Forwards model calls from sandboxes with the right key, within the user's limits."""

    def __init__(
        self, db: Database, vault: Vault, settings: GatewaySettings, chat_is_working: ChatIsWorking
    ) -> None:
        self.db = db
        self.vault = vault
        self.settings = settings
        self.chat_is_working = chat_is_working
        self.key = vault.derive("gateway-token")
        self.ledger = Ledger(db)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30))
        self._settling: set[asyncio.Task[None]] = set()

    def token_for(self, chat: Chat) -> str:
        """The run token a chat's worker uses."""
        return run_token(self.key, chat.id, chat.token_generation)

    def app(self) -> FastAPI:
        """The gateway as an ASGI app."""
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def forward(provider: str, path: str, request: Request) -> Response:
            up = upstream(provider, self.settings.upstreams)
            if up is None:
                return JSONResponse({"error": f"unknown provider {provider}"}, status_code=404)
            try:
                return await self.forward(up, path, request)
            except Refusal as refusal:
                body = error_body(up.kind, refusal.status, str(refusal))
                return JSONResponse(body, status_code=refusal.status)

        return app

    async def _caller(self, request: Request) -> tuple[Chat, User]:
        token = incoming_token(
            {k.lower(): v for k, v in request.headers.items()}, dict(request.query_params)
        )
        parsed = parse_token(self.key, token or "")
        if parsed is None:
            raise Refusal(401, "Forge Web: this is not a valid run token")
        async with self.db.session() as session:
            chat = await session.get(Chat, parsed[0])
            user = await session.get(User, chat.user_id) if chat is not None else None
        if chat is None or user is None or chat.token_generation != parsed[1]:
            raise Refusal(401, "Forge Web: this run token was revoked")
        if user.status != "active" or not self.chat_is_working(chat.id):
            raise Refusal(401, "Forge Web: run tokens only work while their chat runs")
        return chat, user

    async def forward(self, up: Upstream, path: str, request: Request) -> Response:
        """Check, forward and meter one call."""
        chat, user = await self._caller(request)
        if not allowed_path(up.kind, request.method, path):
            raise Refusal(403, f"Forge Web does not forward {request.method} /{path}")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise Refusal(413, "the request is too large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise Refusal(400, "the request body is not JSON") from None
        if not isinstance(body, dict):
            raise Refusal(400, "the request body must be a JSON object")
        key = await self._key(user, up)
        cap = prepare_body(up.kind, path, body, self.settings.max_output_tokens)
        model = model_of(up.kind, path, body)
        reserved = 0.0
        if key.kind == "server":
            reserved = cost(up.name, model, len(raw) // CHARS_PER_TOKEN, cap)
            if not await self.ledger.reserve(user.id, reserved, key.limit_usd or 0.0):
                raise Refusal(403, "Your monthly limit for the server's API keys is used up.")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
        headers.update(auth_headers(up.kind, key.secret), **{"accept-encoding": "identity"})
        params = {k: v for k, v in request.query_params.items() if k != "key"}
        upstream_request = self.client.build_request(
            "POST",
            f"{up.base_url}/{path}",
            params=params,
            headers=headers,
            content=json.dumps(body),
        )
        meta = {
            "user": user.id,
            "chat": chat.id,
            "project": chat.project_id,
            "provider": up.name,
            "model": model,
            "key_kind": key.kind,
            "input_chars": len(raw),
            "reserved": reserved,
        }
        try:
            response = await self.client.send(upstream_request, stream=True)
        except httpx.HTTPError as err:
            self._settle(meta, None, None)
            raise Refusal(
                502, f"the {up.name} API could not be reached: {type(err).__name__}"
            ) from None
        return StreamingResponse(
            self._relay(response, UsageSniffer(up.kind), meta),
            status_code=response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
        )

    async def _key(self, user: User, up: Upstream) -> ChosenKey:
        if up.keyless:
            return ChosenKey(None, "none", None)
        key = await choose_key(self.db, self.vault, user, up.name, self.settings)
        if key is None:
            raise Refusal(
                403,
                f"No API key for {up.name}. Add your own key in Settings, "
                "or ask an admin to let you use the server's keys.",
            )
        return key

    async def _relay(
        self, response: httpx.Response, sniffer: UsageSniffer, meta: dict[str, Any]
    ) -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                sniffer.feed(chunk)
                yield chunk
        finally:
            self._settle(meta, sniffer, response.status_code)
            with contextlib.suppress(Exception):
                await response.aclose()

    def _settle(
        self, meta: dict[str, Any], sniffer: UsageSniffer | None, status: int | None
    ) -> None:
        """Record what a call cost (in the background: the client may be gone)."""
        counted = sniffer.finish() if sniffer is not None else None
        input_tokens = output_tokens = 0
        estimated = False
        if counted is not None and status is not None and status < 400:
            if counted.reported:
                input_tokens, output_tokens = counted.input_tokens, counted.output_tokens
            else:  # an aborted stream: charge what it probably used
                input_tokens = meta["input_chars"] // CHARS_PER_TOKEN
                output_tokens = max(1, (sniffer.output_chars if sniffer else 0) // CHARS_PER_TOKEN)
                estimated = True
        record = UsageRecord(
            user_id=meta["user"], chat_id=meta["chat"], project_id=meta["project"],
            provider=meta["provider"], model=meta["model"], key_kind=meta["key_kind"],
            input_tokens=input_tokens, output_tokens=output_tokens, estimated=estimated,
            cost_usd=cost(meta["provider"], meta["model"], input_tokens, output_tokens),
            created_at=time.time(),
        )  # fmt: skip
        task = asyncio.create_task(self.ledger.settle(record, meta["reserved"]))
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)

    async def close(self) -> None:
        """Finish recording and close the upstream client."""
        if self._settling:
            await asyncio.gather(*self._settling, return_exceptions=True)
        await self.client.aclose()


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the main server."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class PrivateServer:
    """Serves an ASGI app on a unix socket only this user can open (loopback TCP on Windows)."""

    def __init__(self, app: FastAPI, run_dir: Path) -> None:
        self.path = run_dir / "gateway.sock"
        self.port = 0
        self._temp: Path | None = None
        if sys.platform == "win32":
            config = uvicorn.Config(
                app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
            )
        else:
            if len(str(self.path).encode()) > MAX_SOCKET_PATH:
                # Unix socket paths are short (about 100 bytes): use a private temp folder.
                self._temp = Path(tempfile.mkdtemp(prefix="forge-web-"))
                self.path = self._temp / "gateway.sock"
            else:
                run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            config = uvicorn.Config(app, uds=str(self.path), log_level="warning", lifespan="off")
        self.server = _QuietServer(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start serving."""
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        if sys.platform == "win32":
            sock: socket.socket = self.server.servers[0].sockets[0]
            self.port = int(sock.getsockname()[1])
        else:
            self.path.chmod(0o600)

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A new connection to the server."""
        if sys.platform == "win32":
            return await asyncio.open_connection("127.0.0.1", self.port)
        return await asyncio.open_unix_connection(self.path)

    async def stop(self) -> None:
        """Stop serving."""
        self.server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
