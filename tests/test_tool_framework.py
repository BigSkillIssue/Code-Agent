"""Tests for the tool framework: schemas, validation, permissions, hooks, capping, audit."""

import json
from pathlib import Path
from typing import Annotated, Literal

import pytest

from forge import tools
from forge.ctx import Ctx
from forge.hooks import HookOutcome
from forge.ports import Approval
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.errors import ToolError
from support import ScriptedRenderer, make_ctx

GOLDEN = Path(__file__).parent / "fixtures" / "schemas" / "dummy.json"


async def dummy(
    ctx: Ctx,
    path: Annotated[str, "File to read."],
    count: Annotated[int, "How many lines."] = 3,
    mode: Annotated[Literal["fast", "slow"], "Speed."] = "fast",
    tags: Annotated[list[str] | None, "Optional tags."] = None,
    flag: bool = False,
) -> str:
    """Do a dummy thing with a file.

    Only the first docstring line reaches the model.
    """
    return f"{path} x{count} {mode} {tags} {flag}"


async def shout(ctx: Ctx, text: Annotated[str, "Text."]) -> str:
    """Repeat text loudly."""
    if text == "fail":
        raise ToolError("not_found", "nothing to shout", hint="say something")
    if text == "bug":
        raise RuntimeError("unexpected")
    if text == "result":
        return ToolResult(call_id="", ok=False, text="error[no_match]: custom", code="no_match")
    if text == "huge":
        return "x" * 50_000
    if text == "huge-fail":
        raise ToolError("exit_nonzero", "failed", body="y" * 20_000)
    return text.upper()


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the test tools for the duration of one test only."""
    monkeypatch.setitem(tools.REGISTRY, "dummy", tools.make_tool_def(dummy, group="files"))
    monkeypatch.setitem(
        tools.REGISTRY, "shout", tools.make_tool_def(shout, group="shell", permission="ask")
    )


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id="call_7", name=name, arguments=dict(arguments))


def test_schema_matches_golden_file() -> None:
    spec = tools.make_tool_def(dummy, group="files").spec
    assert spec.name == "dummy"
    assert spec.description == "Do a dummy thing with a file."
    assert spec.parameters == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_first_parameter_must_be_ctx() -> None:
    async def bad(path: str) -> str:
        """Bad."""
        return path

    with pytest.raises(TypeError, match="ctx"):
        tools.make_tool_def(bad, group="files")


def test_decorator_registers_into_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "REGISTRY", {})

    @tools.tool(group="search", read_only=True, specifier_arg="path")
    async def finder(ctx: Ctx, path: Annotated[str, "Where."] = ".") -> str:
        """Find things."""
        return path

    tool_def = tools.REGISTRY["finder"]
    assert (tool_def.group, tool_def.read_only, tool_def.specifier_arg) == ("search", True, "path")
    assert tool_def.permission == "auto"


async def test_valid_call_runs(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("dummy", path="a.py", count=2))
    assert result == ToolResult(call_id="call_7", ok=True, text="a.py x2 fast None False")


async def test_invalid_args_name_the_field(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("dummy", count="many"))
    assert not result.ok and result.code == "invalid_args"
    assert result.text.startswith("error[invalid_args]:")
    assert "path" in result.text and "count" in result.text


async def test_unknown_argument_is_rejected(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("dummy", path="a", colour="red"))
    assert result.code == "invalid_args" and "colour" in result.text


async def test_unparsable_arguments_are_reported(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("dummy", _raw_arguments='{"path": '))
    assert result.code == "invalid_args" and "not valid JSON" in result.text


async def test_unknown_tool(ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("nope"))
    assert result.code == "invalid_args" and "unknown tool 'nope'" in result.text


async def test_ask_tool_calls_renderer_approve(registered: None, tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=True)])
    ctx = make_ctx(tmp_project, renderer=renderer)
    result = await tools.call_tool(ctx, call("shout", text="hi"))
    assert result.ok and result.text == "HI"
    assert renderer.approval_requests[0][0].name == "shout"


async def test_declined_approval_returns_feedback(registered: None, tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False, feedback="use echo instead")])
    ctx = make_ctx(tmp_project, renderer=renderer)
    result = await tools.call_tool(ctx, call("shout", text="hi"))
    assert result.code == "permission_denied"
    assert "hint: use echo instead" in result.text


async def test_remembered_approval_is_not_asked_again(registered: None, tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=True, remember=True)])
    ctx = make_ctx(tmp_project, renderer=renderer)
    await tools.call_tool(ctx, call("shout", text="a"))
    await tools.call_tool(ctx, call("shout", text="a"))
    assert len(renderer.approval_requests) == 1


async def test_tool_error_becomes_failed_result(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("shout", text="fail"))
    assert result.code == "not_found"
    assert result.text == "error[not_found]: nothing to shout\nhint: say something"


async def test_returned_tool_result_keeps_its_fields(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("shout", text="result"))
    assert (result.call_id, result.ok, result.code) == ("call_7", False, "no_match")


async def test_unexpected_exception_is_reported_not_raised(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("shout", text="bug"))
    assert not result.ok and result.code == "tool_error"
    assert "RuntimeError" in result.text


async def test_large_output_is_spilled(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("shout", text="huge"))
    assert result.ok
    assert result.spill_path == ".forge/out/call_7.txt"
    preview, note = result.text.rsplit("\n", 1)
    assert preview == "x" * tools.OUTPUT_PREVIEW
    assert note == "[output truncated: 50000 chars total, full output in .forge/out/call_7.txt]"
    assert (ctx.root / ".forge" / "out" / "call_7.txt").read_text() == "x" * 50_000


async def test_large_failure_keeps_head_and_tail(registered: None, ctx: Ctx) -> None:
    result = await tools.call_tool(ctx, call("shout", text="huge-fail"))
    assert not result.ok
    assert len(result.text) < tools.OUTPUT_CAP_FAIL + 100
    assert "chars omitted ...]" in result.text
    assert result.text.startswith("error[exit_nonzero]: failed")
    assert result.text.endswith("y" * 100)


async def test_pre_tool_hook_can_block(
    registered: None, ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocking(event: str, payload: object, c: object = None) -> HookOutcome:
        return HookOutcome(block=event == "pre_tool", message="pushing is not allowed")

    monkeypatch.setattr(ctx.hooks, "run", blocking)
    result = await tools.call_tool(ctx, call("dummy", path="a"))
    assert result.code == "permission_denied"
    assert "pushing is not allowed" in result.text


async def test_audit_log_records_each_call(registered: None, ctx: Ctx) -> None:
    await tools.call_tool(ctx, call("dummy", path="a.py"))
    await tools.call_tool(ctx, call("dummy"))
    lines = (ctx.root / ".forge" / "audit.log").read_text().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert (first["tool"], first["ok"], first["decision"]) == ("dummy", True, "run")
    assert first["args"] == {"path": "a.py"}
    assert (second["ok"], second["code"]) == (False, "invalid_args")


def test_for_role_gives_reviewers_read_only_tools(registered: None) -> None:
    from forge.config import ForgeConfig

    reviewer = {t.name for t in tools.for_role("reviewer", ForgeConfig())}
    coder = {t.name for t in tools.for_role("coder", ForgeConfig())}
    assert "dummy" in coder and "shout" in coder
    assert "shout" not in reviewer
