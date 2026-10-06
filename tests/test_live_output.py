"""Live tool output: shell output reaches the bus while the command runs."""

import time
from pathlib import Path

import pytest

from forge.config import ForgeConfig, SandboxConfig
from forge.events import ToolOutput
from forge.local.local_executor import LocalExecutor
from forge.ports import Command, SandboxPolicy
from forge.providers.base import ToolCall
from forge.runtime.shell import find_shell
from forge.tools import LiveOutput, call_tool
from support import drain, make_ctx

needs_bash = pytest.mark.skipif(find_shell("bash") is None, reason="bash is not installed")
POLICY = SandboxPolicy(mode="full-access")


@needs_bash
async def test_executor_passes_output_before_the_command_ends(tmp_path: Path) -> None:
    executor = LocalExecutor(tmp_path)
    seen: list[tuple[float, str]] = []
    cmd = Command(script="echo one; sleep 0.6; echo two", shell="bash", cwd=str(tmp_path))
    try:
        result = await executor.run(cmd, POLICY, on_output=lambda t: seen.append((time.time(), t)))
    finally:
        await executor.close()
    ended = time.time()
    assert result.stdout.strip() == "one\ntwo"  # the final result is unchanged
    first = next(at for at, text in seen if "one" in text)
    assert ended - first > 0.4
    assert "".join(text for _, text in seen).split() == ["one", "two"]


@needs_bash
async def test_the_bash_tool_publishes_tool_output_events(tmp_project: Path) -> None:
    cfg = ForgeConfig(sandbox=SandboxConfig(mode="full-access"))
    executor = LocalExecutor(tmp_project)
    ctx = make_ctx(tmp_project, cfg=cfg, executor=executor)
    events = ctx.bus.subscribe("*")
    call = ToolCall(id="c7", name="bash", arguments={"command": "echo a; sleep 0.4; echo b"})
    try:
        result = await call_tool(ctx, call)
    finally:
        await executor.close()
    assert result.ok
    live = [e for e in await drain(events) if isinstance(e, ToolOutput)]
    assert live and all(e.call_id == "c7" for e in live)
    assert "".join(e.text for e in live).split() == ["a", "b"]


async def test_live_output_is_throttled_and_masked(ctx: object) -> None:
    from forge.ctx import Ctx

    assert isinstance(ctx, Ctx)
    events = ctx.bus.subscribe("*")
    live = LiveOutput(ctx, "c1", interval_s=0.2)
    for i in range(100):
        live.write(f"{i} ")
    live.write("key sk-ant-api03-" + "x" * 40 + "\n")
    await live.close()
    published = [e for e in await drain(events) if isinstance(e, ToolOutput)]
    assert 1 <= len(published) <= 2
    text = "".join(e.text for e in published)
    assert text.startswith("0 1 2 ") and "[masked secret]" in text and "sk-ant" not in text
