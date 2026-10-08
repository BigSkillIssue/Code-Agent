"""`forge-sandbox attach`: relay this process's stdin/stdout to the daemon's unix socket.

The server runs it with `docker exec -i -u 0 <container> forge-sandbox attach`, so the daemon
outlives any one server connection.
"""

import asyncio
import contextlib
import sys
from pathlib import Path

from forge_sandbox.streams import CHUNK, stdio_streams


async def relay(socket_path: Path) -> int:
    """Copy bytes both ways until either side ends; 1 if the daemon is not reachable."""
    if sys.platform == "win32":
        print("attach needs a POSIX system", file=sys.stderr)
        return 1
    try:
        daemon_reader, daemon_writer = await asyncio.open_unix_connection(socket_path)
    except OSError as err:
        print(f"cannot reach the daemon at {socket_path}: {err.strerror}", file=sys.stderr)
        return 1
    stdin, stdout = await stdio_streams()

    async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        while data := await source.read(CHUNK):
            sink.write(data)
            await sink.drain()
        with contextlib.suppress(OSError, RuntimeError):
            sink.write_eof()

    assert isinstance(stdout, asyncio.StreamWriter)
    tasks = [
        asyncio.create_task(copy(stdin, daemon_writer)),
        asyncio.create_task(copy(daemon_reader, stdout)),
    ]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    return 0
