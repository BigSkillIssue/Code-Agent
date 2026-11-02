"""OS sandbox for shell commands (runtime/sandbox.py) through the LocalExecutor."""

import asyncio
import os
import shutil
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.config import ApprovalConfig, ForgeConfig
from forge.local.local_executor import LocalExecutor
from forge.ports import Approval, Command, SandboxPolicy
from forge.providers.base import ToolCall
from forge.runtime import landlock
from forge.runtime.sandbox import (
    bwrap_prefix,
    is_denied,
    launch_for,
    protected_paths,
    scratch_dir,
    seatbelt_profile,
)
from forge.tools import call_tool
from support import ScriptedRenderer, make_ctx

NO_SANDBOX = launch_for(SandboxPolicy(mode="workspace-write")).mechanism == "none"
needs_sandbox = pytest.mark.skipif(
    NO_SANDBOX, reason="no OS sandbox here (Windows has none yet; Linux needs Landlock or bwrap)"
)
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
async def executor(tmp_project: Path) -> AsyncIterator[LocalExecutor]:
    ex = LocalExecutor(tmp_project)
    yield ex
    await ex.close()


def policy(root: Path, **fields: Any) -> SandboxPolicy:
    return SandboxPolicy(writable_roots=[str(root)], **fields)


def script(root: Path, text: str) -> Command:
    return Command(script=text, shell="bash", cwd=str(root), timeout_s=30)


def test_launch_is_chosen_per_platform() -> None:
    assert launch_for(SandboxPolicy(mode="full-access")).mechanism == "none"
    mechanism = launch_for(SandboxPolicy()).mechanism
    if sys.platform == "darwin":
        assert mechanism == "seatbelt"
    elif sys.platform == "win32":
        assert mechanism == "none"
    else:
        assert mechanism in ("landlock", "bubblewrap", "none")


def test_seatbelt_profile_limits_writes_and_network() -> None:
    profile = seatbelt_profile(["/Users/me/project", "/private/tmp"], network=False)
    assert "(deny file-write*)" in profile
    assert '(subpath "/Users/me/project")' in profile and "(deny network*)" in profile
    assert "(deny network*)" not in seatbelt_profile(["/p"], network=True)


def test_denied_heuristic() -> None:
    ws = SandboxPolicy()
    assert is_denied(ws, 1, "bash: /etc/x: Read-only file system")
    assert is_denied(ws, 7, "curl: (7) Failed to connect to host")
    assert not is_denied(ws, 0, "Permission denied")
    assert not is_denied(ws, 1, "AssertionError")
    assert not is_denied(SandboxPolicy(mode="full-access"), 1, "Permission denied")


@needs_sandbox
@needs_bash
async def test_workspace_write_blocks_writes_outside_the_root(
    executor: LocalExecutor, tmp_project: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = Path.home() / f".forge-sandbox-test-{tmp_project.name}"
    inside = await executor.run(script(tmp_project, "echo hi > inside.txt"), policy(tmp_project))
    assert inside.exit_code == 0 and (tmp_project / "inside.txt").read_text().strip() == "hi"
    result = await executor.run(script(tmp_project, f"echo x > '{outside}'"), policy(tmp_project))
    try:
        assert result.exit_code != 0 and result.sandbox_denied
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@needs_sandbox
@needs_bash
async def test_read_only_blocks_writes_in_the_root(
    executor: LocalExecutor, tmp_project: Path
) -> None:
    result = await executor.run(
        script(tmp_project, "echo x > f.txt"), policy(tmp_project, mode="read-only")
    )
    assert result.sandbox_denied and not (tmp_project / "f.txt").exists()
    reads = await executor.run(script(tmp_project, "ls"), policy(tmp_project, mode="read-only"))
    assert reads.exit_code == 0


async def serve_once() -> tuple[asyncio.Server, int]:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@needs_sandbox
@needs_bash
@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
async def test_network_off_blocks_curl(executor: LocalExecutor, tmp_project: Path) -> None:
    server, port = await serve_once()
    command = f"curl -sS --noproxy '*' -m 5 http://127.0.0.1:{port}/"
    async with server:
        blocked = await executor.run(script(tmp_project, command), policy(tmp_project))
        allowed = await executor.run(
            script(tmp_project, command), policy(tmp_project, network=True)
        )
    assert blocked.exit_code != 0 and blocked.sandbox_denied
    assert allowed.exit_code == 0 and allowed.stdout.strip() == "ok"


@needs_sandbox
@needs_bash
async def test_bash_tool_offers_to_rerun_outside_the_sandbox(tmp_project: Path) -> None:
    outside = Path.home() / f".forge-sandbox-escalate-{tmp_project.name}"
    renderer = ScriptedRenderer(approvals=[Approval(allow=True)])
    ctx = make_ctx(tmp_project, renderer=renderer, executor=LocalExecutor(tmp_project))
    try:
        call = ToolCall(id="c1", name="bash", arguments={"command": f"echo x > '{outside}'"})
        result = await call_tool(ctx, call)
        assert result.ok, result.text
        assert outside.exists()
        assert "without the sandbox" in renderer.approval_requests[-1][1]
    finally:
        outside.unlink(missing_ok=True)
        await ctx.executor.close()  # type: ignore[attr-defined]


@needs_sandbox
@needs_bash
async def test_policy_never_reports_sandbox_denied(tmp_project: Path) -> None:
    outside = Path.home() / f".forge-sandbox-never-{tmp_project.name}"
    cfg = ForgeConfig(approval=ApprovalConfig(policy="never"))
    ctx = make_ctx(tmp_project, cfg=cfg, executor=LocalExecutor(tmp_project))
    try:
        call = ToolCall(id="c1", name="bash", arguments={"command": f"echo x > '{outside}'"})
        result = await call_tool(ctx, call)
        assert result.code == "sandbox_denied" and not outside.exists()
    finally:
        outside.unlink(missing_ok=True)
        await ctx.executor.close()  # type: ignore[attr-defined]


def test_the_scratch_folder_is_private_to_this_process() -> None:
    folder = scratch_dir()
    assert folder == scratch_dir()  # stable for the process
    assert folder.name.startswith("forge-scratch-") and folder.name != "forge-scratch"
    if sys.platform != "win32":
        assert folder.stat().st_mode & 0o777 == 0o700


def test_bubblewrap_isolates_processes_and_keeps_settings_read_only() -> None:
    root = "/work/project"
    args = bwrap_prefix([root, "/tmp", "/dev"], network=False, protected=[f"{root}/.git/config"])
    for flag in ("--unshare-pid", "--unshare-ipc", "--new-session", "--unshare-net"):
        assert flag in args
    bind = args.index(root)
    protect = args.index(f"{root}/.git/config")
    assert args[protect - 1] == "--ro-bind-try" and protect > bind  # later binds win
    assert "--unshare-net" not in bwrap_prefix([root], network=True)


def test_seatbelt_keeps_settings_read_only_and_refuses_apple_events() -> None:
    profile = seatbelt_profile(["/p"], network=True, protected=["/p/.git/hooks"])
    allow = profile.index('(allow file-write* (subpath "/p")')
    deny = profile.index('(deny file-write* (subpath "/p/.git/hooks"))')
    assert deny > allow  # Seatbelt: the later rule wins
    assert "(deny appleevent-send)" in profile
    assert '(literal "/bin/launchctl")' in profile and '(literal "/usr/bin/osascript")' in profile


def test_protected_paths_are_the_settings_in_each_writable_root(tmp_path: Path) -> None:
    paths = protected_paths(SandboxPolicy(writable_roots=[str(tmp_path)]))
    real = Path(os.path.realpath(tmp_path))
    assert str(real / ".git" / "config") in paths and str(real / ".forge" / "config.toml") in paths
    assert protected_paths(SandboxPolicy(mode="read-only", writable_roots=[str(tmp_path)])) == []


def test_landlock_handles_what_the_kernel_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(landlock, "accepted", lambda fs, net, scoped: True)
    fs, net, scoped = landlock.ruleset(9, network=False)
    assert fs & landlock.RESOLVE_UNIX and net == 3 and scoped == landlock.SCOPE_ABSTRACT_UNIX
    fs, net, scoped = landlock.ruleset(5, network=True)
    assert not fs & landlock.RESOLVE_UNIX and net == 0 and scoped == 0
    monkeypatch.setattr(landlock, "accepted", lambda fs, net, scoped: False)
    fs, _, scoped = landlock.ruleset(9, network=False)
    assert not fs & landlock.RESOLVE_UNIX and scoped == 0  # an older kernel says no


@pytest.mark.skipif(landlock.abi() < 4, reason="needs Landlock with network rules")
def test_landlock_starts_the_command_through_forge_not_a_pre_exec_hook(tmp_path: Path) -> None:
    launch = launch_for(SandboxPolicy(writable_roots=[str(tmp_path)]))
    assert launch.mechanism == "landlock"
    assert "__landlock" in launch.prefix and launch.prefix[-1] == "--"


@pytest.mark.skipif(landlock.abi() < 6, reason="needs Landlock scopes (Linux 6.12+)")
async def test_landlock_refuses_abstract_sockets_outside_the_sandbox(
    executor: LocalExecutor, tmp_project: Path
) -> None:
    name = f"forge-test-{os.getpid()}"
    server = socket.socket(socket.AF_UNIX)
    server.bind("\0" + name)
    server.listen(1)
    code = f"import socket; socket.socket(socket.AF_UNIX).connect('\\0{name}')"
    try:
        argv = Command(argv=[sys.executable, "-c", code], cwd=str(tmp_project), timeout_s=30)
        result = await executor.run(argv, policy(tmp_project))
    finally:
        server.close()
    assert result.exit_code != 0 and "not permitted" in result.stderr.lower()


@needs_bash
async def test_commands_do_not_see_forges_model_keys(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_TEST_MODEL_KEY", "sk-secret")
    monkeypatch.setenv("FORGE_TEST_OTHER", "kept")
    executor = LocalExecutor(tmp_project, hidden_env={"FORGE_TEST_MODEL_KEY"})
    show = 'echo "${FORGE_TEST_MODEL_KEY:-none} $FORGE_TEST_OTHER"'
    try:
        shell = await executor.run(script(tmp_project, show), SandboxPolicy(mode="full-access"))
        argv = Command(argv=["bash", "-c", show], cwd=str(tmp_project), timeout_s=30)
        once = await executor.run(argv, SandboxPolicy(mode="full-access"))
    finally:
        await executor.close()
    assert shell.stdout.strip() == once.stdout.strip() == "none kept"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None, reason="needs bwrap"
)
@needs_bash
async def test_bubblewrap_keeps_git_settings_read_only(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(landlock, "abi", lambda: 0)  # bubblewrap even where Landlock exists
    assert launch_for(policy(tmp_project)).mechanism == "bubblewrap"
    before = (tmp_project / ".git" / "config").read_text()
    executor = LocalExecutor(tmp_project)
    try:
        text = "echo x >> .git/config; echo y > ok.txt; ls /proc | grep -c '^[0-9]'"
        result = await executor.run(script(tmp_project, text), policy(tmp_project))
    finally:
        await executor.close()
    assert (tmp_project / ".git" / "config").read_text() == before
    assert (tmp_project / "ok.txt").read_text() == "y\n"
    assert int(result.stdout.split()[-1]) < 10  # its own process namespace
