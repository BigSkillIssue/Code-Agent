"""The sandbox daemon end to end: files, programs, terminals, forwarding, git, reconnects."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from forge_sandbox.daemon import Daemon
from forge_sandbox.mux import OpenFailed
from forge_sandbox.protocol import Notify
from forge_sandbox.rpc import RpcError
from forge_web.containers.local import LocalDriver
from forge_web.sandbox_client import SandboxClient
from support import connect, sandbox

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX sandbox")
LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/net")


async def echo_server() -> tuple[asyncio.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def test_hello_and_file_round_trip(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        assert client.hello is not None
        assert client.hello.info["workspace"] == str(tmp_path.resolve())
        await client.call("fs.write", {"path": "a/b.txt", "text": "hello", "create_dirs": True})
        read = await client.call("fs.read", {"path": "a/b.txt"})
        assert read["text"] == "hello"
        listing = await client.call("fs.list", {"path": "a"})
        assert [e["name"] for e in listing["entries"]] == ["b.txt"]
        await client.call("fs.write", {"path": "c.bin", "base64": "AAE="})
        assert (tmp_path / "c.bin").read_bytes() == b"\0\1"


async def test_bad_calls_are_expected_errors(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        for method, params, code in [
            ("fs.read", {"path": "../etc/passwd"}, "invalid_path"),
            ("fs.read", {"path": 5}, "bad_params"),
            ("fs.read", {"path": "x", "surprise": 1}, "bad_params"),
            ("fs.write", {"path": "x", "text": "a", "base64": "YQ=="}, "bad_params"),
            ("fs.write", {"path": "x", "base64": "not base64!"}, "bad_params"),
            ("procs.output", {"id": "nope"}, "not_found"),
        ]:
            with pytest.raises(RpcError) as err:
                await client.call(method, params)
            assert err.value.code == code, (method, params)


async def test_program_output_and_exit_notification(tmp_path: Path) -> None:
    notes: asyncio.Queue[Notify] = asyncio.Queue()
    async with sandbox(tmp_path, notes=notes) as (_daemon, client):
        script = "print('one'); print('two'); raise SystemExit(3)"
        started = await client.call("procs.start", {"argv": [sys.executable, "-c", script]})
        note = await asyncio.wait_for(notes.get(), 10)
        assert note.method == "procs.exited"
        assert note.params == {"id": started["id"], "exit_code": 3}
        output = await client.call("procs.output", {"id": started["id"]})
        assert output["lines"] == ["one", "two"] and not output["running"]
        later = await client.call("procs.output", {"id": started["id"], "since": 1})
        assert later["lines"] == ["two"]


async def test_stop_a_long_running_program(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        script = "import time\nprint('ready', flush=True)\ntime.sleep(60)"
        started = await client.call("procs.start", {"argv": [sys.executable, "-c", script]})
        stopped = await client.call("procs.stop", {"id": started["id"]}, timeout=20)
        assert stopped["stopped"] != "already exited"
        listed = await client.call("procs.list")
        assert [p["id"] for p in listed] == [started["id"]]


async def test_program_cwd_must_stay_inside(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        with pytest.raises(RpcError) as err:
            await client.call("procs.start", {"argv": ["true"], "cwd": "../.."})
        assert err.value.code == "invalid_path"


@POSIX_ONLY
async def test_terminal_echo_and_resize(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        term = await client.call("pty.create", {"argv": ["/bin/sh"], "cols": 100, "rows": 30})
        channel = await client.open("pty", {"id": term["id"]})
        await channel.send(b"echo forge-$((40+2))\n")
        seen = b""
        while b"forge-42" not in seen:
            seen += await asyncio.wait_for(channel.read(), 10)
        await channel.send_message({"type": "resize", "cols": 120, "rows": 40})
        await asyncio.sleep(0.05)
        listed = await client.call("pty.list")
        assert listed[0]["cols"] == 120 and listed[0]["attached"] == 1
        await client.call("pty.close", {"id": term["id"]})


async def test_attaching_to_a_missing_terminal_is_refused(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        with pytest.raises(OpenFailed) as err:
            await client.open("pty", {"id": "t-missing"})
        assert err.value.info.code == "not_found"


async def test_forward_out_of_the_sandbox(tmp_path: Path) -> None:
    server, port = await echo_server()

    async def to_echo() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", port)

    async with sandbox(tmp_path, targets={"echo": to_echo}) as (_daemon, client):
        listening = await client.call("forward.listen", {"target": "echo", "port": 0})
        reader, writer = await asyncio.open_connection("127.0.0.1", listening["port"])
        writer.write(b"through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 5) == b"through the tunnel"
        writer.close()
        unknown = await client.call("forward.listen", {"target": "nowhere", "port": 0})
        reader, writer = await asyncio.open_connection("127.0.0.1", unknown["port"])
        assert await asyncio.wait_for(reader.read(100), 5) == b""  # refused: closed at once
    server.close()


async def test_connect_into_the_sandbox(tmp_path: Path) -> None:
    server, port = await echo_server()
    async with sandbox(tmp_path) as (_daemon, client):
        channel = await client.open("connect", {"port": port})
        await channel.send(b"preview bytes")
        assert await asyncio.wait_for(channel.read(), 5) == b"preview bytes"
        await channel.close()
        server.close()
        await server.wait_closed()
        with pytest.raises(OpenFailed) as err:
            await client.open("connect", {"port": port})
        assert err.value.info.code == "connect_failed"
        own = await client.call("forward.listen", {"target": "gateway", "port": 0})
        with pytest.raises(OpenFailed) as forbidden:
            await client.open("connect", {"port": own["port"]})
        assert forbidden.value.info.code == "forbidden"


async def test_sandbox_may_only_open_forward_channels(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (daemon, _client):
        with pytest.raises(OpenFailed) as err:
            await daemon.open_to_server("pty", {})
        assert err.value.info.code == "forbidden"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


async def test_git_status_and_diff(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        assert (await client.call("git.status"))["repo"] is False
        git(tmp_path, "init", "-q", "-b", "main")
        git(tmp_path, "config", "user.email", "t@example.com")
        git(tmp_path, "config", "user.name", "T")
        (tmp_path / "app.py").write_text("a = 1\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-q", "-m", "init")
        (tmp_path / "app.py").write_text("a = 2\n")
        (tmp_path / "new.txt").write_text("new\n")
        status = await client.call("git.status")
        assert status["repo"] and status["branch"] == "main"
        files = {f["path"]: (f["index"], f["worktree"]) for f in status["files"]}
        assert files == {"app.py": (" ", "M"), "new.txt": ("?", "?")}
        diff = await client.call("git.diff", {"path": "app.py"})
        assert "-a = 1" in diff["diff"] and "+a = 2" in diff["diff"]


async def test_git_files_lists_and_finds_project_files(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (_daemon, client):
        assert (await client.call("git.files", {}))["files"] == []  # not a repository yet
        git(tmp_path, "init", "-q", "-b", "main")
        for name in ("src/app.py", "src/utils/helpers.py", "docs/apple.md", "README.md",
                     "build/out.js", "debug.log", "has space.txt"):  # fmt: skip
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text("x\n")
        (tmp_path / ".gitignore").write_text("build/\n*.log\n")
        listed = await client.call("git.files", {})
        assert set(listed["files"]) == {
            ".gitignore",
            "README.md",
            "docs/apple.md",
            "has space.txt",
            "src/app.py",
            "src/utils/helpers.py",
        }
        found = await client.call("git.files", {"query": "app"})
        assert found["files"][:2] == ["src/app.py", "docs/apple.md"]  # names before paths
        assert (await client.call("git.files", {"query": "suh"}))["files"] == [
            "src/utils/helpers.py"
        ]  # letters in order
        limited = await client.call("git.files", {"limit": 2})
        assert len(limited["files"]) == 2 and limited["total"] == 6


@LINUX_ONLY
async def test_listening_ports_leave_out_the_daemons_own(tmp_path: Path) -> None:
    server, port = await echo_server()
    async with sandbox(tmp_path) as (_daemon, client):
        own = await client.call("forward.listen", {"target": "gateway", "port": 0})
        ports = {p["port"] for p in await client.call("ports.list")}
        assert port in ports and own["port"] not in ports
    server.close()


async def test_a_new_connection_replaces_the_old_and_state_survives(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path, env=dict(os.environ))
    first, first_serving = await connect(daemon)
    script = "import time; time.sleep(30)"
    started = await first.call("procs.start", {"argv": [sys.executable, "-c", script]})
    second, second_serving = await connect(daemon)
    await asyncio.wait_for(first.wait_closed(), 5)
    listed = await second.call("procs.list")
    assert [p["id"] for p in listed] == [started["id"]] and listed[0]["running"]
    await second.close()
    for task in (first_serving, second_serving):
        task.cancel()
    await daemon.close()


async def test_local_driver_runs_a_real_daemon_with_a_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_WEB_SECRET_FOR_TEST", "must-not-leak")
    driver = LocalDriver(tmp_path)
    client = SandboxClient(await driver.connect("demo-1"))
    hello = await client.start()
    try:
        assert Path(hello.info["workspace"]) == driver.workspace("demo-1").resolve()
        await client.call("fs.write", {"path": "x.txt", "text": "from the server"})
        assert (driver.workspace("demo-1") / "x.txt").read_text() == "from the server"
        script = (
            "import os\n"
            "print(os.environ.get('FORGE_WEB_SECRET_FOR_TEST'))\n"
            "print(os.environ['FORGE_HOME'])"
        )
        started = await client.call("procs.start", {"argv": [sys.executable, "-c", script]})
        output: dict[str, Any] = {"running": True}
        while output["running"]:
            await asyncio.sleep(0.05)
            output = await client.call("procs.output", {"id": started["id"]})
        assert output["lines"][0] == "None"
        assert Path(output["lines"][1]) == driver.project_dir("demo-1") / "forge-home"
    finally:
        await client.close()
    await driver.remove("demo-1")
    assert not driver.project_dir("demo-1").exists()


def test_local_driver_refuses_unsafe_project_ids(tmp_path: Path) -> None:
    from forge_web.containers.driver import SandboxError

    driver = LocalDriver(tmp_path)
    for bad in ("../x", "A", "", "a/b"):
        with pytest.raises(SandboxError):
            driver.project_dir(bad)
