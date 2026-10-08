"""WebSockets of a preview (hot reload and the app's own): browser ↔ proxy ↔ app.

The proxy makes its own handshake with the app over a `connect` channel (offering the browser's
subprotocols, no extensions), accepts the browser with the subprotocol the app chose, and then
moves whole messages both ways; `websockets`' sans-I/O protocol does the framing to the app.
"""

import asyncio
import base64
import contextlib
import secrets
from typing import Any

import httpcore
from starlette.types import Message, Receive, Scope, Send
from websockets.frames import Frame, Opcode
from websockets.protocol import Protocol, Side, State

from forge_web.preview_auth import Target
from forge_web.preview_headers import upstream_headers
from forge_web.preview_upstream import TIMEOUTS, upstream
from forge_web.services import Services

MAX_MESSAGE = 16 * 1024 * 1024
SENDABLE = range(1000, 5000)
UNSENDABLE = frozenset({1004, 1005, 1006, 1015})  # reserved: never sent in a close frame


async def relay_websocket(
    services: Services, target: Target, scope: Scope, receive: Receive, send: Send
) -> None:
    """Connect the browser's WebSocket to the app's, or refuse it if the app does not accept."""
    if (await receive())["type"] != "websocket.connect":
        return
    connection = upstream(services, target)
    try:
        response = await connection.handle_async_request(handshake(scope, target))
        if response.status != 101:
            await response.aclose()
            await send({"type": "websocket.close", "code": 4404})
            return
        answer = {name.lower(): value for name, value in response.headers}  # as the app wrote them
        chosen = answer.get(b"sec-websocket-protocol")
        await send({"type": "websocket.accept",
                    "subprotocol": chosen.decode("latin-1") if chosen else None})  # fmt: skip
        stream = response.extensions["network_stream"]
        await relay(stream, receive, send)
    except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError):
        with contextlib.suppress(Exception):
            await send({"type": "websocket.close", "code": 1011})
    finally:
        await connection.aclose()


def handshake(scope: Scope, target: Target) -> httpcore.Request:
    """The upgrade request the app gets."""
    headers = upstream_headers(scope["headers"], target, websocket=True)
    headers += [
        (b"connection", b"Upgrade"), (b"upgrade", b"websocket"),
        (b"sec-websocket-version", b"13"),
        (b"sec-websocket-key", base64.b64encode(secrets.token_bytes(16))),
    ]  # fmt: skip
    offered = scope.get("subprotocols") or []
    if offered:
        headers.append((b"sec-websocket-protocol", ", ".join(offered).encode("latin-1")))
    path = scope.get("raw_path") or scope["path"].encode("latin-1")
    query = scope.get("query_string", b"")
    url = httpcore.URL(scheme=b"http", host=b"localhost", port=target.port,
                       target=bytes(path) + (b"?" + query if query else b""))  # fmt: skip
    return httpcore.Request(b"GET", url, headers=headers, extensions={"timeout": TIMEOUTS})


async def relay(stream: Any, receive: Receive, send: Send) -> None:
    """Move messages both ways until one side closes."""
    protocol = Protocol(Side.CLIENT, state=State.OPEN, max_size=MAX_MESSAGE)
    lock = asyncio.Lock()
    tasks = [
        asyncio.create_task(from_browser(protocol, lock, stream, receive)),
        asyncio.create_task(from_app(protocol, lock, stream, send)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def flush(protocol: Protocol, lock: asyncio.Lock, stream: Any) -> None:
    """Write what the protocol has queued for the app (frames, pongs, the closing frame)."""
    async with lock:
        for data in protocol.data_to_send():
            if data:
                await stream.write(data)


async def from_browser(
    protocol: Protocol, lock: asyncio.Lock, stream: Any, receive: Receive
) -> None:
    """The browser's messages to the app; a close when the browser leaves."""
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            code = int(message.get("code", 1000))
            if code in SENDABLE and code not in UNSENDABLE:
                protocol.send_close(code)
            else:
                protocol.send_close()
            await flush(protocol, lock, stream)
            return
        if message.get("text") is not None:
            protocol.send_text(message["text"].encode("utf-8"))
        elif message.get("bytes") is not None:
            protocol.send_binary(message["bytes"])
        await flush(protocol, lock, stream)


class Messages:
    """Puts the app's frames together into whole messages for the browser."""

    def __init__(self) -> None:
        self.kind, self.parts = Opcode.BINARY, list[bytes]()

    def add(self, frame: Frame) -> Message | None:
        """The finished message this frame completes, if any."""
        if frame.opcode in (Opcode.TEXT, Opcode.BINARY):
            self.kind, self.parts = frame.opcode, [bytes(frame.data)]
        elif frame.opcode is Opcode.CONT:
            self.parts.append(bytes(frame.data))
        else:
            return None  # pings are answered by the protocol, closes end the relay
        if not frame.fin:
            return None
        body, self.parts = b"".join(self.parts), []
        if self.kind is Opcode.TEXT:
            return {"type": "websocket.send", "text": body.decode("utf-8", "replace")}
        return {"type": "websocket.send", "bytes": body}


async def from_app(protocol: Protocol, lock: asyncio.Lock, stream: Any, send: Send) -> None:
    """The app's messages to the browser; a close when the app closes or goes away."""
    messages = Messages()
    while protocol.close_rcvd is None:
        data = await stream.read(65536)
        if data:
            protocol.receive_data(data)
        else:
            protocol.receive_eof()
        await flush(protocol, lock, stream)
        for event in protocol.events_received():
            message = messages.add(event) if isinstance(event, Frame) else None
            if message is not None:
                await send(message)
        if not data or protocol.state is State.CLOSED:
            break
    code = protocol.close_rcvd.code if protocol.close_rcvd is not None else 1000
    await send({"type": "websocket.close",
                "code": code if code in SENDABLE and code not in UNSENDABLE else 1000})  # fmt: skip
