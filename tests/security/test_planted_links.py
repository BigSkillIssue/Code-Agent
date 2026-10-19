"""Links that a sandboxed command plants in `.forge/` or the project must not redirect Forge's
own (unsandboxed) writes and reads, and a tampered undo journal must not write outside."""

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from forge.commands import run_command
from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.files import journal_dir
from forge.runtime.shell import find_shell
from forge.tools import call_tool
from support import make_ctx

can_symlink = pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks need admin rights on Windows"
)
no_bash = pytest.mark.skipif(find_shell("bash") is None, reason="bash is not installed")


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("top secret\n")
    return folder


def plant(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link, target_is_directory=target.is_dir())


@can_symlink
@no_bash
async def test_a_planted_job_log_link_is_not_written_through(
    tmp_project: Path, outside: Path
) -> None:
    executor = LocalExecutor(tmp_project.resolve())
    ctx = make_ctx(tmp_project, executor=executor)
    plant(ctx.root / ".forge" / "jobs" / "j1.log", outside / "secret.txt")
    try:
        result = await run(ctx, "bash", command="echo pwned", background=True)
        if result.ok:
            await executor.jobs["j1"].task  # type: ignore[misc]
    finally:
        await executor.close()
    assert (outside / "secret.txt").read_text() == "top secret\n"


@can_symlink
async def test_spilled_output_does_not_follow_a_planted_folder(ctx: Ctx, outside: Path) -> None:
    plant(ctx.root / ".forge" / "out", outside)
    await run(ctx, "write_file", path="big.txt", content=("y" * 200 + "\n") * 300)
    result = await run(ctx, "read_file", path="big.txt", limit=300)
    assert result.ok and len(result.text) < 31_000
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


@can_symlink
async def test_the_audit_log_does_not_follow_a_planted_link(ctx: Ctx, outside: Path) -> None:
    plant(ctx.root / ".forge" / "audit.log", outside / "secret.txt")
    await run(ctx, "list_dir", path=".")
    assert (outside / "secret.txt").read_text() == "top secret\n"


@can_symlink
async def test_the_undo_journal_does_not_follow_a_planted_link(ctx: Ctx, outside: Path) -> None:
    plant(journal_dir(ctx), outside)
    await run(ctx, "write_file", path="a.txt", content="x")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


async def test_undo_never_writes_outside_the_project(ctx: Ctx, outside: Path) -> None:
    folder = journal_dir(ctx)
    folder.mkdir(parents=True)
    record = {str(outside / "secret.txt"): base64.b64encode(b"pwned\n").decode()}
    (folder / "journal.jsonl").write_text(json.dumps(record) + "\n")
    reply = await run_command(ctx, "/undo")
    assert (outside / "secret.txt").read_text() == "top secret\n"
    assert "outside" in reply


async def test_undo_never_writes_protected_folders(ctx: Ctx) -> None:
    folder = journal_dir(ctx)
    folder.mkdir(parents=True)
    target = ctx.root / ".forge" / "config.toml"
    record = {str(target): base64.b64encode(b"[hooks]\n").decode()}
    (folder / "journal.jsonl").write_text(json.dumps(record) + "\n")
    await run_command(ctx, "/undo")
    assert not target.exists()


@can_symlink
async def test_a_folder_swapped_for_a_link_after_the_check_is_not_written_through(
    ctx: Ctx, outside: Path
) -> None:
    from forge.runtime import files

    (ctx.root / "sub").mkdir()
    original = files.check_writable

    def check_then_swap(root: Path, roots: list[Path], path: Path) -> None:
        original(root, roots, path)
        (ctx.root / "sub").rmdir()
        os.symlink(outside, ctx.root / "sub", target_is_directory=True)

    files.check_writable = check_then_swap  # type: ignore[assignment]
    try:
        await run(ctx, "write_file", path="sub/new.txt", content="pwned")
    finally:
        files.check_writable = original  # type: ignore[assignment]
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


@pytest.mark.parametrize("path", [".GIT/config", ".Forge/config.toml", "sub/.Git/hooks/x"])
async def test_protected_folders_in_any_case(ctx: Ctx, path: str) -> None:
    # macOS and Windows file systems ignore case: `.GIT/config` is `.git/config` there
    assert (await run(ctx, "write_file", path=path, content="x")).code == "protected_path"


@can_symlink
async def test_a_review_diff_does_not_read_a_new_link_out_of_the_project(
    tmp_project: Path, outside: Path
) -> None:
    from forge.runtime.gitops import diff_since

    os.symlink(outside / "secret.txt", tmp_project / "notes.txt")
    diff = await diff_since(tmp_project)
    assert "top secret" not in diff
    assert "notes.txt" in diff
