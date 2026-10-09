"""Many channels over one byte stream, with credit-based flow control.

Every channel may carry raw bytes (DATA) and JSON messages. A sender may only send as many payload
bytes as the receiver granted (WINDOW at first, then CREDIT frames as the receiver's application
consumes what arrived), so one slow or stalled channel never blocks the others, and a peer that
sends more than it was granted breaks the protocol. Management frames and the control channel jump
the write queue.

Channel ids: 0 is the control channel; the server opens odd ids, the daemon even ids.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from forge_sandbox import __version__
from forge_sandbox.frames import (
    CONTROL_CHANNEL,
    MANAGEMENT,
    Frame,
    FrameType,
    ProtocolError,
    credit_amount,
    credit_frame,
    read_frame,
    split_payload,
)
from forge_sandbox.protocol import (
    PROTOCOL_VERSION,
    ErrorInfo,
    Hello,
    OpenRequest,
    from_json,
    parse_control,
    parse_error,
    parse_open,
    to_json,
)

WINDOW = 1024 * 1024
MAX_CREDIT = 1 << 31
MAX_CHANNELS = 256
# Management and control frames need no credit. This many waiting means the peer stopped reading
# (while it keeps sending): the connection closes instead of growing without end.
MAX_URGENT = 4096
log = logging.getLogger(__name__)
Role = Literal["server", "daemon"]


class ChannelClosed(Exception):
    """The channel (or the whole connection) is closed."""


class ProtocolMismatch(ProtocolError):
    """The two sides speak different protocol versions."""


class OpenRefused(Exception):
    """Raised by an open handler to refuse a channel; the opener gets `info`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.info = ErrorInfo(code=code, message=message)


class OpenFailed(Exception):
    """The peer refused to open a channel."""

    def __init__(self, info: ErrorInfo) -> None:
        super().__init__(f"{info.code}: {info.message}")
        self.info = info


class Writer(Protocol):
    """The writing half of a byte stream (asyncio.StreamWriter fits)."""

    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


Item = bytes | dict[str, Any]
OpenHandler = Callable[["Channel"], Awaitable[None]]


class Channel:
    """One logical stream: send and receive bytes or JSON messages."""

    def __init__(self, mux: "Mux", channel_id: int, request: OpenRequest) -> None:
        self.id = channel_id
        self.kind = request.kind
        self.args = request.args
        self._mux = mux
        self._inbox: asyncio.Queue[tuple[Item, int] | None] = asyncio.Queue()
        self._send_window = mux.window  # bytes we may still send
        self._recv_allowance = mux.window  # bytes the peer may still send
        self._to_credit = 0  # consumed bytes not yet granted back
        self._credit = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._parts: list[bytes] = []
        self._parts_size = 0
        self.accepted = False
        self.local_closed = False
        self.remote_closed = False

    # sending ---------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        """Send raw bytes (waits while the peer has granted no room)."""
        async with self._send_lock:
            for piece in split_payload(data):
                if piece:
                    await self._send_frame(FrameType.DATA, piece)

    async def send_message(self, message: Any) -> None:
        """Send one JSON message (a pydantic model or a dict)."""
        raw = to_json(message)
        if len(raw) > self._mux.max_message:
            raise ValueError(f"message of {len(raw)} bytes exceeds {self._mux.max_message}")
        pieces = split_payload(raw)
        async with self._send_lock:
            for index, piece in enumerate(pieces):
                last = index == len(pieces) - 1
                await self._send_frame(FrameType.MESSAGE if last else FrameType.MESSAGE_MORE, piece)

    async def _send_frame(self, kind: FrameType, piece: bytes) -> None:
        while self._send_window < len(piece):
            if self.local_closed or self._mux.closed:
                raise ChannelClosed(f"channel {self.id} is closed")
            self._credit.clear()
            await self._credit.wait()
        if self.local_closed or self._mux.closed:
            raise ChannelClosed(f"channel {self.id} is closed")
        self._send_window -= len(piece)
        self._mux.enqueue(Frame(self.id, kind, piece))

    # receiving -------------------------------------------------------------

    async def receive(self) -> Item:
        """The next bytes or message; ChannelClosed after the peer closed its side."""
        entry = await self._inbox.get()
        if entry is None:
            self._inbox.put_nowait(None)  # keep reporting the end
            raise ChannelClosed(f"channel {self.id} ended")
        item, size = entry
        self._consumed(size)
        return item

    async def read(self) -> bytes:
        """The next bytes, or b"" at the end of the stream."""
        try:
            item = await self.receive()
        except ChannelClosed:
            return b""
        if not isinstance(item, bytes):
            raise ProtocolError(f"expected bytes on channel {self.id}, got a message")
        return item

    async def recv_message(self) -> dict[str, Any]:
        """The next JSON message; ChannelClosed at the end."""
        item = await self.receive()
        if not isinstance(item, dict):
            raise ProtocolError(f"expected a message on channel {self.id}, got bytes")
        return item

    def _consumed(self, size: int) -> None:
        self._to_credit += size
        self._maybe_credit()

    def _maybe_credit(self) -> None:
        # Granting whenever nothing is left to read keeps a message that is larger than the rest
        # of the window from waiting forever for credit the reader would only give after it.
        if self.remote_closed or self._to_credit == 0:
            return
        if self._to_credit >= self._mux.window // 2 or self._inbox.empty():
            self._recv_allowance += self._to_credit
            self._mux.enqueue(credit_frame(self.id, self._to_credit))
            self._to_credit = 0

    # closing ---------------------------------------------------------------

    async def accept(self) -> None:
        """Confirm a channel the peer opened (open handlers call this first)."""
        if not self.accepted:
            self.accepted = True
            self._mux.enqueue(Frame(self.id, FrameType.OPEN_OK))

    async def close(self) -> None:
        """Tell the peer we send nothing more; the channel is gone once both sides closed."""
        if self.local_closed:
            return
        self.local_closed = True
        self._credit.set()
        if not self._mux.closed:
            self._mux.enqueue(Frame(self.id, FrameType.CLOSE))
        self._mux.forget_if_done(self)

    # called by the mux -----------------------------------------------------

    def _deliver(self, frame: Frame) -> None:
        self._recv_allowance -= len(frame.payload)
        if self._recv_allowance < 0:
            raise ProtocolError(f"peer sent more than it was granted on channel {self.id}")
        if frame.type is FrameType.DATA:
            if frame.payload:
                self._inbox.put_nowait((frame.payload, len(frame.payload)))
            return
        self._parts.append(frame.payload)
        self._parts_size += len(frame.payload)
        if self._parts_size > self._mux.max_message:
            raise ProtocolError(f"message on channel {self.id} exceeds {self._mux.max_message}")
        if frame.type is FrameType.MESSAGE:
            message = from_json(b"".join(self._parts))
            self._inbox.put_nowait((message, self._parts_size))
            self._parts, self._parts_size = [], 0
        else:
            self._maybe_credit()

    def _grant(self, amount: int) -> None:
        self._send_window = min(self._send_window + amount, MAX_CREDIT)
        self._credit.set()

    def _ended(self) -> None:
        self.remote_closed = True
        self._inbox.put_nowait(None)


class Mux:
    """Both ends of a connection run one Mux: it reads frames, writes frames, routes channels."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: Writer,
        *,
        role: Role,
        on_open: OpenHandler | None = None,
        window: int = WINDOW,
        max_message: int | None = None,
        max_channels: int = MAX_CHANNELS,
        info: dict[str, Any] | None = None,
    ) -> None:
        self.role = role
        self.max_channels = max_channels
        self.window = window
        self.max_message = min(max_message or window, window)
        self.info = info or {}
        self.peer: Hello | None = None
        self.error: Exception | None = None
        self.closed = False
        self._reader = reader
        self._writer = writer
        self._on_open = on_open
        self._next_id = 1 if role == "server" else 2
        self._channels: dict[int, Channel] = {}
        self._opening: dict[int, asyncio.Future[None]] = {}
        self._urgent: deque[Frame] = deque()
        self._normal: deque[Frame] = deque()
        self._overflow = False
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._done = asyncio.Event()
        self.control = Channel(self, CONTROL_CHANNEL, OpenRequest(kind="control"))
        self.control.accepted = True
        self._channels[CONTROL_CHANNEL] = self.control

    async def start(self, timeout: float = 30) -> Hello:
        """Exchange Hello messages and start the read and write loops; returns the peer's Hello."""
        self._spawn(self._write_loop())
        self._spawn(self._read_loop())
        hello = Hello(role=self.role, version=__version__, info=self.info)
        await self.control.send_message(hello)
        try:
            first = parse_control(await asyncio.wait_for(self.control.recv_message(), timeout))
        except (ChannelClosed, TimeoutError, ProtocolError) as err:
            await self.close(err if isinstance(err, ProtocolError) else ProtocolError(str(err)))
            raise self.error or ProtocolError("handshake failed") from err
        if not isinstance(first, Hello):
            await self.close(ProtocolError("the first message must be hello"))
            raise ProtocolError("the first message must be hello")
        if first.protocol != PROTOCOL_VERSION:
            mismatch = ProtocolMismatch(
                f"peer speaks protocol {first.protocol}, we {PROTOCOL_VERSION}"
            )
            await self.close(mismatch)
            raise mismatch
        self.peer = first
        return first

    async def open(self, kind: str, args: dict[str, Any] | None = None) -> Channel:
        """Open a channel on the peer; OpenFailed if the peer refuses."""
        if self.closed:
            raise ChannelClosed("the connection is closed")
        channel_id = self._next_id
        self._next_id += 2
        request = OpenRequest(kind=kind, args=args or {})
        channel = Channel(self, channel_id, request)
        self._channels[channel_id] = channel
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._opening[channel_id] = waiter
        self.enqueue(Frame(channel_id, FrameType.OPEN, to_json(request)))
        try:
            await waiter
        except BaseException:
            self._channels.pop(channel_id, None)
            raise
        finally:
            self._opening.pop(channel_id, None)
        channel.accepted = True
        return channel

    def enqueue(self, frame: Frame) -> None:
        """Queue a frame for writing (management and control frames first)."""
        if self.closed:
            return
        urgent = frame.type in MANAGEMENT or frame.channel == CONTROL_CHANNEL
        if urgent and len(self._urgent) >= MAX_URGENT:
            if not self._overflow:
                self._overflow = True
                self._spawn(self.close(ProtocolError("the peer stopped reading")))
            return
        (self._urgent if urgent else self._normal).append(frame)
        self._wake.set()

    def forget_if_done(self, channel: Channel) -> None:
        """Drop a channel once both sides closed it."""
        if channel.local_closed and channel.remote_closed and channel.id != CONTROL_CHANNEL:
            self._channels.pop(channel.id, None)

    async def wait_closed(self) -> None:
        """Wait until the connection is closed."""
        await self._done.wait()

    async def close(self, error: Exception | None = None) -> None:
        """Close the connection: every channel ends, waiting opens fail."""
        if self.closed:
            return
        self.closed = True
        self.error = self.error or error
        for waiter in self._opening.values():
            if not waiter.done():
                waiter.set_exception(ChannelClosed("the connection closed"))
        for channel in list(self._channels.values()):
            channel.local_closed = True
            channel._credit.set()
            if not channel.remote_closed:
                channel._ended()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
        self._done.set()

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_loop(self) -> None:
        try:
            while True:
                await self._wake.wait()
                batch = 0
                while (self._urgent or self._normal) and batch < 32:
                    frame = self._urgent.popleft() if self._urgent else self._normal.popleft()
                    self._writer.write(frame.encode())
                    batch += 1
                if not (self._urgent or self._normal):
                    self._wake.clear()
                await self._writer.drain()
        except (ConnectionError, OSError) as err:
            await self.close(err)

    async def _read_loop(self) -> None:
        try:
            while (frame := await read_frame(self._reader)) is not None:
                self._dispatch(frame)
            await self.close()
        except ProtocolError as err:
            await self.close(err)
        except (ConnectionError, OSError) as err:
            await self.close(err)

    def _dispatch(self, frame: Frame) -> None:
        if frame.type is FrameType.OPEN:
            self._peer_opened(frame)
            return
        if frame.type in (FrameType.OPEN_OK, FrameType.OPEN_FAIL):
            self._open_answered(frame)
            return
        channel = self._channels.get(frame.channel)
        if channel is None:
            return  # frames still in flight for a channel we already dropped
        if frame.type is FrameType.CREDIT:
            channel._grant(credit_amount(frame))
        elif frame.type is FrameType.CLOSE:
            channel._ended()
            self.forget_if_done(channel)
        elif not channel.remote_closed:
            channel._deliver(frame)

    def _peer_opened(self, frame: Frame) -> None:
        peer_parity = 0 if self.role == "server" else 1
        if frame.channel == 0 or frame.channel % 2 != peer_parity:
            raise ProtocolError(f"peer opened channel {frame.channel} with the wrong parity")
        if frame.channel in self._channels:
            raise ProtocolError(f"peer reopened channel {frame.channel}")
        request = parse_open(frame.payload)
        if len(self._channels) >= self.max_channels:
            info = ErrorInfo(code="too_many_channels", message="too many open channels")
            self.enqueue(Frame(frame.channel, FrameType.OPEN_FAIL, to_json(info)))
            return
        channel = Channel(self, frame.channel, request)
        self._channels[frame.channel] = channel
        self._spawn(self._serve_channel(channel))

    async def _serve_channel(self, channel: Channel) -> None:
        try:
            if self._on_open is None:
                raise OpenRefused("not_supported", "this side opens no channels")
            await self._on_open(channel)
            if not channel.accepted:
                raise OpenRefused("refused", f"channel kind {channel.kind!r} was not accepted")
        except OpenRefused as refused:
            self._refuse(channel, refused.info)
        except (ChannelClosed, ProtocolError, ConnectionError):
            pass
        except Exception:
            log.exception("handler for channel %s (%s) failed", channel.id, channel.kind)
            if not channel.accepted:
                self._refuse(channel, ErrorInfo(code="internal", message="the channel failed"))
        finally:
            await channel.close()

    def _refuse(self, channel: Channel, info: ErrorInfo) -> None:
        self.enqueue(Frame(channel.id, FrameType.OPEN_FAIL, to_json(info)))
        channel.local_closed = channel.remote_closed = True
        self._channels.pop(channel.id, None)

    def _open_answered(self, frame: Frame) -> None:
        waiter = self._opening.get(frame.channel)
        if waiter is None or waiter.done():
            return
        if frame.type is FrameType.OPEN_OK:
            waiter.set_result(None)
        else:
            waiter.set_exception(OpenFailed(parse_error(frame.payload)))
