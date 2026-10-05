"""OS sandbox for shell commands (runtime/sandbox.py) through the LocalExecutor."""

import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.config import ApprovalConfig, ForgeConfig
from forge.local.local_executor import LocalExecutor
from forge.ports import Approval, Command, SandboxPolicy
from forge.providers.base import ToolCall
from forge.runtime.sandbox import is_denied, launch_for, seatbelt_profile
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
