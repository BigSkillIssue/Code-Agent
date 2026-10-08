"""Terminals: a shell on a pseudo-terminal that any number of channels can attach to.

Output is kept as scrollback for channels that attach later. When the slowest attached channel
falls more than PAUSE_AT bytes behind, the daemon stops reading the terminal, so the kernel slows
the program down instead of the daemon buffering without limit.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any

from forge_sandbox.fsops import Workspace
from forge_sandbox.methods import (
    EmptyParams,
    ProcParams,
    PtyAttachArgs,
    PtyCreateParams,
    PtyResize,
    PtyResizeParams,
    method,
    parse_params,
)
from forge_sandbox.mux import Channel, ChannelClosed
from forge_sandbox.procs import owner_kwargs, workspace_dir
from forge_sandbox.rpc import Handler, RpcError

if sys.platform != "win32":
    import fcntl
    import termios

SCROLLBACK = 256 * 1024
PAUSE_AT = 512 * 1024
MAX_TERMINALS = 16
TERM_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}


class Listener:
    """One attached channel and the output still on its way to it."""

    def __init__(self, terminal: "Terminal", channel: Channel) -> None:
        self.terminal = terminal
        self.channel = channel
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pending = 0
        self.task = asyncio.create_task(self._send())

    def push(self, data: bytes) -> None:
        """Queue output for this channel."""
        self.pending += len(data)
        self.queue.put_nowait(data)

    def finish(self) -> None:
        """Close the channel once the queued output is sent (the terminal has ended)."""
        self.queue.put_nowait(None)

    async def _send(self) -> None:
        with contextlib.suppress(ChannelClosed):
            while (data := await self.queue.get()) is not None:
                await self.channel.send(data)
                self.pending -= len(data)
                self.terminal.maybe_resume()
            await self.channel.close()


class Terminal:
    """A program on a pseudo-terminal (POSIX only)."""

    def __init__(self, process: subprocess.Popen[bytes], master: int, cols: int, rows: int) -> None:
        self.id = f"t{secrets.token_hex(4)}"
        self.process = process
        self.master = master
        self.cols, self.rows = cols, rows
        self.started_at = time.time()
        self.scrollback = bytearray()
        self.listeners: set[Listener] = set()
        self.exit_code: int | None = None
        self.paused = False
        self._outbox = bytearray()
        self._loop = asyncio.get_running_loop()
        os.set_blocking(master, False)
        self._loop.add_reader(master, self._readable)

    def info(self) -> dict[str, Any]:
        """What pty.list reports."""
        return {
            "id": self.id,
            "cols": self.cols,
            "rows": self.rows,
            "started_at": self.started_at,
            "running": self.exit_code is None,
            "exit_code": self.exit_code,
            "attached": len(self.listeners),
        }

    def _readable(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except OSError:  # EIO: the program and everything it started have gone
            data = b""
        if not data:
            self._loop.remove_reader(self.master)
            self._loop.create_task(self._ended())
            return
        self._broadcast(data)
        if self.listeners and max(lis.pending for lis in self.listeners) > PAUSE_AT:
            self._loop.remove_reader(self.master)
            self.paused = True

    def maybe_resume(self) -> None:
        """Read again once every attached channel caught up."""
        behind = max((lis.pending for lis in self.listeners), default=0)
        if self.paused and behind <= PAUSE_AT // 2 and self.exit_code is None:
            self.paused = False
            self._loop.add_reader(self.master, self._readable)

    def _broadcast(self, data: bytes) -> None:
        self.scrollback += data
        del self.scrollback[:-SCROLLBACK]
        for listener in self.listeners:
            listener.push(data)

    async def _ended(self) -> None:
        self.exit_code = await asyncio.to_thread(self.process.wait)
        self._broadcast(f"\r\n[process exited with code {self.exit_code}]\r\n".encode())
        for listener in self.listeners:
            listener.finish()

    def write(self, data: bytes) -> None:
        """Type into the terminal (buffered when the terminal is not ready for more)."""
        if self.exit_code is not None:
            return
        self._outbox += data
        self._flush()

    def _flush(self) -> None:
        try:
            written = os.write(self.master, self._outbox)
        except BlockingIOError:
            written = 0
        except OSError:
            self._outbox.clear()
            return
        del self._outbox[:written]
        self._loop.remove_writer(self.master)
        if self._outbox:
            self._loop.add_writer(self.master, self._flush)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the program the terminal's new size."""
        self.cols, self.rows = cols, rows
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def close(self) -> None:
        """Hang up the terminal and stop its program."""
        self._loop.remove_reader(self.master)
        self._loop.remove_writer(self.master)
        if self.exit_code is None and sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGHUP)
        with contextlib.suppress(OSError):
            os.close(self.master)
        for listener in self.listeners:
            listener.task.cancel()
            with contextlib.suppress(ChannelClosed):
                await listener.channel.close()


def _controlling_terminal() -> None:
    """Runs in the child: make the pseudo-terminal its controlling terminal."""
    if sys.platform != "win32":
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Ptys:
    """Creates terminals and attaches channels to them."""

    def __init__(self, workspace: Workspace, env: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.env = {**env, **TERM_ENV}
        self.terminals: dict[str, Terminal] = {}

    def handlers(self) -> dict[str, Handler]:
        """pty.* methods."""
        return {
            "pty.create": method(PtyCreateParams, self.create),
            "pty.list": method(EmptyParams, self.list),
            "pty.resize": method(PtyResizeParams, self.resize),
            "pty.close": method(ProcParams, self.close_one),
        }

    async def create(self, params: PtyCreateParams) -> dict[str, Any]:
        """Start a shell (or `argv`) on a new terminal."""
        if sys.platform == "win32":
            raise RpcError("not_supported", "terminals need a POSIX sandbox (use Docker isolation)")
        if sum(t.exit_code is None for t in self.terminals.values()) >= MAX_TERMINALS:
            raise RpcError("too_many", f"at most {MAX_TERMINALS} terminals")
        argv = params.argv or [shutil.which("bash") or "/bin/sh", "-l"]
        cwd = workspace_dir(self.workspace, params.cwd)
        master, slave = os.openpty()
        try:
            if self.workspace.owner is not None and os.geteuid() == 0:
                os.fchown(slave, self.workspace.owner.uid, self.workspace.owner.gid)
            process = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=self.env,
                start_new_session=True,
                preexec_fn=_controlling_terminal,  # only an ioctl in the child
                **owner_kwargs(self.workspace.owner),
            )
        except OSError as err:
            os.close(master)
            raise RpcError("start_failed", f"could not start {argv[0]}: {err.strerror}") from None
        finally:
            os.close(slave)
        terminal = Terminal(process, master, params.cols, params.rows)
        terminal.resize(params.cols, params.rows)
        self.terminals[terminal.id] = terminal
        return terminal.info()

    async def list(self, _params: EmptyParams) -> list[dict[str, Any]]:
        """Every terminal, newest first."""
        return [t.info() for t in sorted(self.terminals.values(), key=lambda t: -t.started_at)]

    async def resize(self, params: PtyResizeParams) -> dict[str, Any]:
        """Change a terminal's size."""
        terminal = self._get(params.id)
        terminal.resize(params.cols, params.rows)
        return terminal.info()

    async def close_one(self, params: ProcParams) -> dict[str, Any]:
        """Close a terminal and stop its program."""
        terminal = self._get(params.id)
        await terminal.close()
        self.terminals.pop(terminal.id, None)
        return {"id": terminal.id}

    async def attach(self, channel: Channel) -> None:
        """Serve a `pty` channel: scrollback first, then live output; bytes in are typed."""
        terminal = self._get(parse_params(PtyAttachArgs, channel.args).id)
        await channel.accept()
        listener = Listener(terminal, channel)
        if terminal.scrollback:
            listener.push(bytes(terminal.scrollback))
        if terminal.exit_code is not None:
            listener.finish()
        terminal.listeners.add(listener)
        try:
            while True:
                item = await channel.receive()
                if isinstance(item, bytes):
                    terminal.write(item)
                else:
                    size = parse_params(PtyResize, item)
                    terminal.resize(size.cols, size.rows)
        except (ChannelClosed, RpcError):
            pass
        finally:
            terminal.listeners.discard(listener)
            listener.task.cancel()
            terminal.maybe_resume()

    def _get(self, terminal_id: str) -> Terminal:
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise RpcError("not_found", f"no terminal {terminal_id}")
        return terminal

    async def close(self) -> None:
        """Close every terminal."""
        for terminal in list(self.terminals.values()):
            await terminal.close()
        self.terminals.clear()
