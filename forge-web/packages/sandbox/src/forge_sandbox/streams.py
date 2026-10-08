"""Byte-stream helpers shared by the daemon and the server: stdio as asyncio streams, pumping."""

import asyncio
import contextlib
import os
import queue
import sys
import threading

from forge_sandbox.mux import Channel, ChannelClosed

CHUNK = 64 * 1024


async def pump(
    channel: Channel, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Copy bytes both ways between a channel and a connection until both directions end."""

    async def upstream() -> None:  # connection -> channel
        try:
            while data := await reader.read(CHUNK):
                await channel.send(data)
        finally:
            await channel.close()

    async def downstream() -> None:  # channel -> connection
        try:
            while data := await channel.read():
                writer.write(data)
                await writer.drain()
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                if writer.can_write_eof():
                    writer.write_eof()

    await asyncio.gather(upstream(), downstream(), return_exceptions=True)
    writer.close()
    with contextlib.suppress(OSError, ConnectionError):
        await writer.wait_closed()


class ThreadWriter:
    """A Writer over a blocking file descriptor, written from a thread (Windows pipes)."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._loop = asyncio.get_running_loop()
        self._idle = asyncio.Event()
        self._idle.set()
        threading.Thread(target=self._run, daemon=True).start()

    def write(self, data: bytes) -> None:
        """Queue bytes for the thread."""
        self._idle.clear()
        self._queue.put(data)

    async def drain(self) -> None:
        """Wait until everything queued was written."""
        await self._idle.wait()

    def close(self) -> None:
        """Stop the thread after the queued bytes."""
        self._queue.put(None)

    def _run(self) -> None:
        while (data := self._queue.get()) is not None:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._fd, view) :]
            except OSError:
                return
            if self._queue.empty():
                self._loop.call_soon_threadsafe(self._idle.set)


def _feed_from_thread(reader: asyncio.StreamReader, fd: int) -> None:
    loop = asyncio.get_running_loop()

    def run() -> None:
        while True:
            try:
                data = os.read(fd, CHUNK)
            except OSError:
                data = b""
            if not data:
                loop.call_soon_threadsafe(reader.feed_eof)
                return
            loop.call_soon_threadsafe(reader.feed_data, data)

    threading.Thread(target=run, daemon=True).start()


async def stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter | ThreadWriter]:
    """This process's stdin and stdout as an asyncio reader and writer."""
    reader = asyncio.StreamReader(limit=2**20)
    if sys.platform == "win32":
        # Proactor loops cannot do overlapped I/O on anonymous pipes: use threads instead.
        _feed_from_thread(reader, sys.stdin.fileno())
        return reader, ThreadWriter(sys.stdout.fileno())
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


async def channel_closed_quietly(channel: Channel) -> None:
    """Close a channel, ignoring a connection that is already gone."""
    with contextlib.suppress(ChannelClosed, ConnectionError):
        await channel.close()
