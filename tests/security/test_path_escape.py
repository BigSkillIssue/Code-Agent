"""Sandbox escape attempts through paths: symlinks, '..', absolute paths, protected folders."""

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from forge.ctx import Ctx
from forge.ports import Approval
from forge.providers.base import ToolCall, ToolResult
from forge.tools import call_tool
from support import ScriptedRenderer, make_ctx

can_symlink = pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks need admin rights on Windows"
)


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("top secret\n")
    return folder


@pytest.mark.parametrize("path", ["../outside/new.txt", "../../x.txt"])
async def test_dotdot_writes_are_refused(ctx: Ctx, path: str) -> None:
    assert (await run(ctx, "write_file", path=path, content="x")).code == "outside_root"


async def test_absolute_writes_are_refused(ctx: Ctx, outside: Path) -> None:
    result = await run(ctx, "write_file", path=str(outside / "new.txt"), content="x")
    assert result.code == "outside_root" and not (outside / "new.txt").exists()


@can_symlink
async def test_symlinked_file_cannot_be_written_through(ctx: Ctx, outside: Path) -> None:
    os.symlink(outside / "secret.txt", ctx.root / "link.txt")
    result = await run(ctx, "write_file", path="link.txt", content="pwned")
    assert result.code == "outside_root"
    assert (outside / "secret.txt").read_text() == "top secret\n"


@can_symlink
async def test_symlinked_folder_cannot_be_written_through(ctx: Ctx, outside: Path) -> None:
    os.symlink(outside, ctx.root / "linkdir", target_is_directory=True)
    write = await run(ctx, "write_file", path="linkdir/new.txt", content="x")
    patch = "*** Begin Patch\n*** Add File: linkdir/p.txt\n+x\n*** End Patch"
    assert write.code == "outside_root"
    assert (await run(ctx, "apply_patch", patch=patch)).code == "outside_root"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


async def test_protected_folders(ctx: Ctx) -> None:
    for path in (".git/config", ".forge/config.toml", "sub/../.git/hooks/pre-commit"):
        assert (await run(ctx, "write_file", path=path, content="x")).code == "protected_path", path


async def test_reading_outside_the_project_needs_approval(tmp_project: Path, outside: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    ctx = make_ctx(tmp_project, renderer=renderer)
    result = await run(ctx, "read_file", path=str(outside / "secret.txt"))
    assert result.code == "permission_denied" and "top secret" not in result.text
    assert "outside the project" in renderer.approval_requests[0][1]
    inside = tmp_project / "ok.txt"
    inside.write_text("fine\n")
    assert (await run(ctx, "read_file", path="ok.txt")).ok and len(renderer.approval_requests) == 1


@can_symlink
async def test_reading_through_a_symlink_out_needs_approval(
    tmp_project: Path, outside: Path
) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    ctx = make_ctx(tmp_project, renderer=renderer)
    os.symlink(outside / "secret.txt", tmp_project / "innocent.txt")
    result = await run(ctx, "read_file", path="innocent.txt")
    assert result.code == "permission_denied"
