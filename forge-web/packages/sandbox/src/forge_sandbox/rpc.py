"""Request/response calls and one-way notifications over the control channel."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from forge_sandbox.frames import ProtocolError
from forge_sandbox.mux import Channel, ChannelClosed
from forge_sandbox.protocol import ErrorInfo, Hello, Notify, Request, Response, parse_control

log = logging.getLogger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[Any]]
NotifyHandler = Callable[[Notify], Awaitable[None]]


class RpcError(Exception):
    """An expected failure of a call: `code` for programs, the message for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Rpc:
    """Calls the peer and answers the peer's calls with `handlers` (method name -> handler)."""

    def __init__(
        self,
        channel: Channel,
        handlers: Mapping[str, Handler] | None = None,
        on_notify: NotifyHandler | None = None,
        *,
        max_concurrency: int = 32,
    ) -> None:
        self.channel = channel
        self.handlers = dict(handlers or {})
        self.on_notify = on_notify
        self._next_id = 1
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._slots = asyncio.Semaphore(max_concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> Any:
        """Call `method` on the peer and return its result; RpcError if it failed."""
        request_id = self._next_id
        self._next_id += 1
        waiter: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = waiter
        try:
            await self.channel.send_message(
                Request(id=request_id, method=method, params=params or {})
            )
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self._waiting.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a one-way message."""
        await self.channel.send_message(Notify(method=method, params=params or {}))

    async def run(self) -> None:
        """Handle incoming messages until the channel ends; then fail every waiting call."""
        try:
            while True:
                message = parse_control(await self.channel.recv_message())
                self._handle(message)
        except (ChannelClosed, ProtocolError):
            pass
        finally:
            for waiter in self._waiting.values():
                if not waiter.done():
                    waiter.set_exception(ChannelClosed("the connection closed"))
            for task in self._tasks:
                task.cancel()

    def _handle(self, message: Hello | Request | Response | Notify) -> None:
        if isinstance(message, Response):
            waiter = self._waiting.get(message.id)
            if waiter is not None and not waiter.done():
                if message.ok:
                    waiter.set_result(message.result)
                else:
                    error = message.error or ErrorInfo(code="error", message="the call failed")
                    waiter.set_exception(RpcError(error.code, error.message))
        elif isinstance(message, Request):
            self._spawn(self._answer(message))
        elif isinstance(message, Notify) and self.on_notify is not None:
            self._spawn(self.on_notify(message))
        elif isinstance(message, Hello):
            raise ProtocolError("hello sent twice")

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, request: Request) -> None:
        async with self._slots:
            response = await self._dispatch(request)
        with contextlib.suppress(ChannelClosed):
            await self.channel.send_message(response)

    async def _dispatch(self, request: Request) -> Response:
        handler = self.handlers.get(request.method)
        if handler is None:
            error = ErrorInfo(code="unknown_method", message=f"no method {request.method!r}")
            return Response(id=request.id, ok=False, error=error)
        try:
            result = await handler(request.params)
        except RpcError as err:
            return Response(
                id=request.id, ok=False, error=ErrorInfo(code=err.code, message=err.message)
            )
        except Exception:
            # Details stay in this side's log: the peer may be untrusted.
            log.exception("method %s failed", request.method)
            error = ErrorInfo(code="internal", message=f"{request.method} failed")
            return Response(id=request.id, ok=False, error=error)
        return Response(id=request.id, ok=True, result=result)
