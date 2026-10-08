"""The sandbox daemon: serves one project's workspace to the server over a multiplexed stream.

Its state (programs, terminals, listeners and, from W04, chats) lives as long as the daemon, not
as long as a connection: when the server reconnects (say after a restart), the new connection
replaces the old one and everything is still there.

Two ways to serve:
- `--stdio`: one connection on stdin/stdout; the daemon ends with it (local isolation mode).
- `--socket PATH`: a unix socket only root can open (the daemon is PID 1 of a container, and
  `forge-sandbox attach` relays a `docker exec` pipe to it).
"""

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_sandbox import __version__
from forge_sandbox.chats import Chats
from forge_sandbox.forward import Forwards
from forge_sandbox.fsops import Owner, Workspace
from forge_sandbox.gitinfo import GitInfo
from forge_sandbox.gitops import GitOps
from forge_sandbox.methods import (
    DeleteParams,
    EmptyParams,
    PathParams,
    PortsParams,
    ReadParams,
    RenameParams,
    UnzipParams,
    WriteParams,
    WritePartParams,
    method,
)
from forge_sandbox.mux import Channel, ChannelClosed, Mux, OpenRefused, Writer
from forge_sandbox.netinfo import listening_ports
from forge_sandbox.procs import Procs
from forge_sandbox.pty import Ptys
from forge_sandbox.rpc import Handler, Rpc, RpcError
from forge_sandbox.streams import stdio_streams
from forge_sandbox.unzip import UnzipLimits, unzip
from forge_sandbox.usage import disk_usage

log = logging.getLogger(__name__)


@dataclass
class Connection:
    """The server connection currently attached."""

    mux: Mux
    rpc: Rpc


class Daemon:
    """Everything one workspace offers the server."""

    def __init__(
        self, root: Path, *, owner: Owner | None = None, env: Mapping[str, str] | None = None
    ) -> None:
        self.workspace = Workspace(root, owner)
        self.env = dict(os.environ if env is None else env)
        self.boot = secrets.token_hex(8)  # changes with every daemon start: chat numbers restart
        self.procs = Procs(self.workspace, self.env, self.notify)
        self.ptys = Ptys(self.workspace, self.env)
        self.git = GitInfo(self.workspace, self.env)
        self.gitops = GitOps(self.workspace, self.env)
        self.forwards = Forwards(self.open_to_server)
        self.chats = Chats(self.workspace, self.env)
        self.connection: Connection | None = None

    def info(self) -> dict[str, Any]:
        """What the daemon tells the server in its hello."""
        return {
            "workspace": str(self.workspace.root),
            "platform": sys.platform,
            "pid": os.getpid(),
            "version": __version__,
            "boot": self.boot,
        }

    def handlers(self) -> dict[str, Handler]:
        """Every control-channel method."""
        return {
            **self.fs_handlers(),
            **self.procs.handlers(),
            **self.ptys.handlers(),
            **self.git.handlers(),
            **self.gitops.handlers(),
            **self.forwards.handlers(),
            **self.chats.handlers(),
            "ports.list": method(PortsParams, self.ports),
            "daemon.info": method(EmptyParams, self.describe),
        }

    def fs_handlers(self) -> dict[str, Handler]:
        """fs.* methods; the blocking file work runs in a thread."""
        ws = self.workspace

        async def fs_list(p: PathParams) -> Any:
            return await asyncio.to_thread(ws.list, p.path)

        async def fs_stat(p: PathParams) -> Any:
            return await asyncio.to_thread(ws.stat, p.path)

        async def fs_read(p: ReadParams) -> Any:
            return await asyncio.to_thread(ws.read, p.path, p.limit, p.offset)

        async def fs_write_part(p: WritePartParams) -> Any:
            data = decode_base64(p.base64)
            return await asyncio.to_thread(
                lambda: ws.write_part(p.path, p.upload, data, last=p.last,
                                      create_dirs=p.create_dirs, abort=p.abort)
            )  # fmt: skip

        async def fs_write(p: WriteParams) -> Any:
            data = decode_write(p)
            return await asyncio.to_thread(
                lambda: ws.write(
                    p.path, data, create_dirs=p.create_dirs, expected_mtime=p.expected_mtime
                )
            )

        async def fs_mkdir(p: PathParams) -> Any:
            return await asyncio.to_thread(ws.mkdir, p.path)

        async def fs_rename(p: RenameParams) -> Any:
            return await asyncio.to_thread(ws.rename, p.src, p.dst)

        async def fs_unzip(p: UnzipParams) -> Any:
            limits = UnzipLimits()
            if p.max_bytes is not None:
                limits = UnzipLimits(max_total=min(limits.max_total, p.max_bytes))
            return await asyncio.to_thread(
                lambda: unzip(ws, p.path, p.dest, strip_root=p.strip_root, limits=limits)
            )

        async def fs_usage(_p: EmptyParams) -> Any:
            return await asyncio.to_thread(disk_usage, ws)

        async def fs_delete(p: DeleteParams) -> Any:
            return await asyncio.to_thread(lambda: ws.delete(p.path, recursive=p.recursive))

        return {
            "fs.list": method(PathParams, fs_list),
            "fs.stat": method(PathParams, fs_stat),
            "fs.read": method(ReadParams, fs_read),
            "fs.write": method(WriteParams, fs_write),
            "fs.write_part": method(WritePartParams, fs_write_part),
            "fs.unzip": method(UnzipParams, fs_unzip),
            "fs.usage": method(EmptyParams, fs_usage),
            "fs.mkdir": method(PathParams, fs_mkdir),
            "fs.rename": method(RenameParams, fs_rename),
            "fs.delete": method(DeleteParams, fs_delete),
        }

    async def ports(self, params: PortsParams) -> list[dict[str, Any]]:
        """Ports programs in the sandbox listen on (the daemon's own are left out)."""
        owner = os.getpid() if params.owned else None
        return await asyncio.to_thread(listening_ports, self.forwards.ports(), owner)

    async def describe(self, _params: EmptyParams) -> dict[str, Any]:
        """The daemon's hello info, on request."""
        return self.info()

    async def on_open(self, channel: Channel) -> None:
        """Serve a channel the server opened."""
        try:
            if channel.kind == "chat":
                await self.chats.attach(channel)
            elif channel.kind == "pty":
                await self.ptys.attach(channel)
            elif channel.kind == "connect":
                await self.forwards.connect(channel)
            else:
                raise OpenRefused("unknown_kind", f"no channel kind {channel.kind!r}")
        except RpcError as err:
            if channel.accepted:
                return
            raise OpenRefused(err.code, err.message) from None

    async def notify(self, method_name: str, params: dict[str, Any]) -> None:
        """Tell the attached server something (dropped when none is attached)."""
        if self.connection is not None:
            with contextlib.suppress(ChannelClosed, ConnectionError):
                await self.connection.rpc.notify(method_name, params)

    async def open_to_server(self, kind: str, args: dict[str, Any]) -> Channel:
        """Open a channel to the attached server."""
        if self.connection is None:
            raise ChannelClosed("no server is attached")
        return await self.connection.mux.open(kind, args)

    async def serve_connection(self, reader: asyncio.StreamReader, writer: Writer) -> None:
        """Serve one server connection until it ends; a newer connection replaces it."""
        mux = Mux(reader, writer, role="daemon", on_open=self.on_open, info=self.info())
        try:
            await mux.start()
        except Exception as err:
            log.warning("handshake failed: %s", err)
            return
        connection = Connection(mux, Rpc(mux.control, self.handlers()))
        previous, self.connection = self.connection, connection
        if previous is not None:
            await previous.mux.close()
        try:
            await connection.rpc.run()
        finally:
            if self.connection is connection:
                self.connection = None
            await mux.close()

    async def close(self) -> None:
        """Stop chats, programs, terminals and listeners."""
        await self.chats.close()
        await self.procs.close()
        await self.ptys.close()
        await self.forwards.close()


def decode_write(params: WriteParams) -> bytes:
    """The bytes an fs.write call wants written."""
    if (params.text is None) == (params.base64 is None):
        raise RpcError("bad_params", "give either text or base64")
    if params.text is not None:
        return params.text.encode("utf-8")
    return decode_base64(params.base64 or "")


def decode_base64(text: str) -> bytes:
    """Bytes from base64; a bad_params error if it is not valid."""
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise RpcError("bad_params", "base64 is not valid") from None


async def serve_stdio(daemon: Daemon) -> None:
    """Serve the one connection on stdin/stdout, then stop."""
    reader, writer = await stdio_streams()
    try:
        await daemon.serve_connection(reader, writer)
    finally:
        await daemon.close()


async def serve_socket(daemon: Daemon, path: Path) -> None:
    """Serve connections on a unix socket that only this user can open, forever."""
    if sys.platform == "win32":
        raise SystemExit("unix sockets need a POSIX system; use --stdio")
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    previous = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(daemon.serve_connection, path)
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()
