"""remember and recall (docs/TOOLS.md: Memory)."""

import datetime
from pathlib import Path
from typing import Any

import pytest

from forge.ctx import Ctx
from forge.ports import Approval, Session
from forge.providers.base import ToolCall, ToolResult, text_message
from forge.tools import call_tool
from support import ScriptedRenderer, make_ctx


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


TODAY = datetime.date.today().isoformat()


async def test_remember_creates_file_and_heading(ctx: Ctx) -> None:
    result = await run(ctx, "remember", note="Run tests with\nuv run pytest")
    assert result.ok and result.text == 'remembered in FORGE.md: "Run tests with uv run pytest"'
    text = (ctx.root / "FORGE.md").read_text()
    assert text == (
        f"# FORGE.md\n\n## Notes from Forge\n\n- Run tests with uv run pytest (added {TODAY})\n"
    )


async def test_remember_appends_under_existing_heading(ctx: Ctx) -> None:
    (ctx.root / "FORGE.md").write_text(
        "# FORGE.md\n\nUse tabs.\n\n## Notes from Forge\n\n- a (added 2026-01-01)\n"
    )
    assert (await run(ctx, "remember", note="b")).ok
    lines = (ctx.root / "FORGE.md").read_text().splitlines()
    assert lines[-2:] == ["- a (added 2026-01-01)", f"- b (added {TODAY})"]
    assert lines.count("## Notes from Forge") == 1


async def test_duplicate_note_is_skipped(ctx: Ctx) -> None:
    await run(ctx, "remember", note="same")
    result = await run(ctx, "remember", note="same")
    assert result.ok and "already remembered" in result.text
    assert (ctx.root / "FORGE.md").read_text().count("- same") == 1


async def test_remember_always_asks_and_decline_is_permission_denied(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    ctx = make_ctx(tmp_project, renderer=renderer)
    result = await run(ctx, "remember", note="x")
    assert result.code == "permission_denied" and len(renderer.approval_requests) == 1
    assert not (tmp_project / "FORGE.md").exists()


async def test_remember_user_scope(
    ctx: Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    assert (await run(ctx, "remember", note="prefer uv", scope="user")).ok
    assert "- prefer uv" in (tmp_path / "home" / "FORGE.md").read_text()


async def add_session(ctx: Ctx, session_id: str, created: float, *texts: str) -> None:
    session = Session(
        id=session_id,
        project_root=str(ctx.root),
        created_at=created,
        status="done",
        messages=[text_message("user", t) for t in texts],
    )
    await ctx.store.save_session(session)


async def test_recall_finds_earlier_sessions_but_not_the_current_one(ctx: Ctx) -> None:
    await add_session(ctx, "a" * 32, 1_000_000.0, "the refresh token lives in an httpOnly cookie")
    await add_session(ctx, "b" * 32, 2_000_000.0, "token token token rotation")
    await add_session(ctx, ctx.session.id, 3_000_000.0, "token in the current session")
    result = await run(ctx, "recall", query="token")
    assert result.ok
    lines = result.text.splitlines()
    assert lines[0].startswith("1. session bbbbbbbb")  # more matches ranks first
    assert lines[2].startswith("2. session aaaaaaaa")
    assert "current session" not in result.text


async def test_recall_without_hits_and_bad_args(ctx: Ctx) -> None:
    result = await run(ctx, "recall", query="nothing")
    assert result.ok and result.text == 'no earlier sessions mention "nothing"'
    assert (await run(ctx, "recall", query=" ")).code == "invalid_args"
    assert (await run(ctx, "recall", query="x", limit=21)).code == "invalid_args"
