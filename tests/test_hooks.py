"""Hooks: shell hooks from [hooks], Python hooks via @hook, and the events they run on."""

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from forge import hooks as hooks_module
from forge.config import ForgeConfig, HookConfig
from forge.ctx import Ctx
from forge.events import ErrorEvent
from forge.hooks import HookOutcome, Hooks, hook
from forge.local.memory_store import MemoryStore
from forge.pipeline import run_task
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider
from forge.tools import call_tool
from forge.wiring import BUILTIN_FAKE, install_fake, open_session
from support import NoExecutor, ScriptedRenderer, drain, make_ctx

PY = Path(sys.executable).as_posix()

FORMATTER = """
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text().upper())
"""

GUARD = """
import json, sys
event = json.load(sys.stdin)
command = event.get("args", {}).get("command", "")
if event["tool"] == "bash" and command.startswith("git push"):
    print("pushing is not allowed in this repository", file=sys.stderr)
    sys.exit(2)
"""


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return f'"{PY}" "{path.as_posix()}"'


def hook_ctx(root: Path, **hooks: list[HookConfig]) -> Ctx:
    cfg = ForgeConfig(hooks=hooks)  # type: ignore[arg-type]
    ctx = make_ctx(root, cfg=cfg)
    ctx.hooks = Hooks(cfg, python_hooks=[])
    return ctx


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def test_post_tool_formatter_runs_after_edits(tmp_project: Path, tmp_path: Path) -> None:
    formatter = HookConfig(
        match="edit_file|write_file", command=script(tmp_path, "fmt.py", FORMATTER) + " {path}"
    )
    ctx = hook_ctx(tmp_project, post_tool=[formatter])
    assert (await run(ctx, "write_file", path="notes.txt", content="hello\n")).ok
    assert (tmp_project / "notes.txt").read_text() == "HELLO\n"
    assert (await run(ctx, "read_file", path="notes.txt")).ok  # read_file does not match
    assert (await run(ctx, "edit_file", path="notes.txt", old="HELLO", new="bye")).ok
    assert (tmp_project / "notes.txt").read_text() == "BYE\n"


async def test_pre_tool_blocks_git_push_and_the_model_sees_why(
    tmp_project: Path, tmp_path: Path
) -> None:
    guard = HookConfig(match="bash", command=script(tmp_path, "guard.py", GUARD))
    ctx = hook_ctx(tmp_project, pre_tool=[guard])
    blocked = await run(ctx, "bash", command="git push origin main")
    assert blocked.code == "permission_denied"
    assert "blocked by a pre_tool hook" in blocked.text
    assert "pushing is not allowed in this repository" in blocked.text


async def test_hanging_hook_times_out_and_the_session_goes_on(tmp_project: Path) -> None:
    sleeper = HookConfig(command=f'"{PY}" -c "import time; time.sleep(30)"')
    ctx = hook_ctx(tmp_project, pre_tool=[sleeper])
    ctx.hooks.timeout_s = 0.5
    events = ctx.bus.subscribe("*")
    (tmp_project / "a.txt").write_text("x\n")
    started = time.monotonic()
    result = await run(ctx, "read_file", path="a.txt")
    assert result.ok and time.monotonic() - started < 10
    warnings = [e.message for e in await drain(events) if isinstance(e, ErrorEvent)]
    assert any("timed out after 0.5s" in w for w in warnings)


async def test_failing_hook_is_only_a_warning(tmp_project: Path) -> None:
    ctx = hook_ctx(tmp_project, pre_tool=[HookConfig(command=f'"{PY}" -c "raise SystemExit(1)"')])
    events = ctx.bus.subscribe("*")
    (tmp_project / "a.txt").write_text("x\n")
    assert (await run(ctx, "read_file", path="a.txt")).ok
    assert any("exit code 1" in e.message for e in await drain(events) if isinstance(e, ErrorEvent))


async def test_python_hooks_and_session_events(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_module, "PYTHON_HOOKS", [])
    seen: list[str] = []

    @hook("session_start")
    def started(event: str, payload: dict[str, Any], ctx: Any) -> None:
        seen.append(event)

    @hook("prompt_submit")
    async def submitted(event: str, payload: dict[str, Any], ctx: Any) -> None:
        seen.append(f"{event}:{payload['prompt']}")

    @hook("stop")
    def stopped(event: str, payload: dict[str, Any], ctx: Any) -> None:
        seen.append(f"{event}:{payload['ok']}")

    cfg = ForgeConfig()
    install_fake(cfg, FakeProvider.from_data(BUILTIN_FAKE))
    ctx = await open_session(
        tmp_project, cfg, ScriptedRenderer(), store=MemoryStore(), executor=NoExecutor()
    )
    report = await run_task("say hello", ctx)
    assert report.ok
    assert seen == ["session_start", "prompt_submit:say hello", "stop:True"]


async def test_prompt_submit_hook_can_stop_a_task(tmp_project: Path) -> None:
    def refuse(event: str, payload: dict[str, Any], ctx: Any) -> HookOutcome:
        return HookOutcome(block=True, message="no work on Fridays")

    ctx = make_ctx(tmp_project)
    ctx.hooks = Hooks(
        ForgeConfig(), python_hooks=[hooks_module.PythonHook("prompt_submit", "", refuse)]
    )
    report = await run_task("do something", ctx)
    assert (
        not report.ok
        and report.summary == "A prompt_submit hook stopped this task: no work on Fridays"
    )
