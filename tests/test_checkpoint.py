"""Tests for working-tree snapshots and /undo."""

from pathlib import Path

from forge.commands import run_command
from forge.plan import Plan, Step, TaskSpec
from forge.runtime.checkpoint import ref_name, restore, snapshot
from support import git, make_ctx

SPEC = TaskSpec(goal="g", context="c", requirements=[], acceptance_criteria=["a"], size="small")


def committed_repo(root: Path) -> None:
    (root / "app.py").write_text("v0\n")
    (root / "keep.txt").write_text("keep\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")


async def test_restore_brings_back_the_earlier_tree(tmp_project: Path) -> None:
    committed_repo(tmp_project)
    (tmp_project / "app.py").write_text("v1\n")
    ref = await snapshot(tmp_project, "sess", "s3")
    assert ref == ref_name("sess", "s3")
    (tmp_project / "app.py").write_text("v2\n")
    (tmp_project / "new" / "deep").mkdir(parents=True)
    (tmp_project / "new" / "deep" / "file.py").write_text("x\n")
    (tmp_project / "keep.txt").unlink()
    changed = await restore(tmp_project, ref)
    assert (tmp_project / "app.py").read_text() == "v1\n"
    assert (tmp_project / "keep.txt").read_text() == "keep\n"
    assert not (tmp_project / "new").exists()
    assert {"app.py", "keep.txt", "new/deep/file.py"} <= set(changed)


async def test_untracked_files_are_in_the_snapshot(tmp_project: Path) -> None:
    committed_repo(tmp_project)
    (tmp_project / "notes.md").write_text("draft\n")
    ref = await snapshot(tmp_project, "sess", "s1")
    (tmp_project / "notes.md").write_text("changed\n")
    assert ref is not None
    await restore(tmp_project, ref)
    assert (tmp_project / "notes.md").read_text() == "draft\n"


async def test_head_and_index_are_untouched(tmp_project: Path) -> None:
    committed_repo(tmp_project)
    (tmp_project / "staged.py").write_text("s\n")
    git(tmp_project, "add", "staged.py")
    head, status = git(tmp_project, "rev-parse", "HEAD"), git(tmp_project, "status", "--porcelain")
    ref = await snapshot(tmp_project, "sess", "s1")
    assert ref is not None
    await restore(tmp_project, ref)
    assert git(tmp_project, "rev-parse", "HEAD") == head
    assert git(tmp_project, "status", "--porcelain") == status
    assert "forge" not in git(tmp_project, "branch", "-a")


async def test_works_before_the_first_commit(tmp_project: Path) -> None:
    (tmp_project / "a.txt").write_text("1\n")
    ref = await snapshot(tmp_project, "sess", "s1")
    (tmp_project / "a.txt").write_text("2\n")
    assert ref is not None
    await restore(tmp_project, ref)
    assert (tmp_project / "a.txt").read_text() == "1\n"


async def test_no_git_no_snapshot(tmp_path: Path) -> None:
    assert await snapshot(tmp_path, "sess", "s1") is None


async def test_undo_after_step_3_equals_the_step_2_tree(tmp_project: Path) -> None:
    committed_repo(tmp_project)
    ctx = make_ctx(tmp_project)
    steps = [
        Step(id=f"s{i}", title=f"t{i}", detail="", check="true", status="done") for i in (1, 2, 3)
    ]
    ctx.session.plan = Plan(spec=SPEC, steps=steps)
    for number in (1, 2, 3):
        ref = await snapshot(tmp_project, ctx.session.id, f"s{number}")
        assert ref is not None
        ctx.state.checkpoints[f"s{number}"] = ref
        (tmp_project / "app.py").write_text(f"after step {number}\n")
    reply = await run_command(ctx, "/undo")
    assert reply.startswith("undid step s3")
    assert (tmp_project / "app.py").read_text() == "after step 2\n"
    assert ctx.session.plan.steps[2].status == "todo"


async def test_undo_without_checkpoints_uses_the_file_journal(tmp_project: Path) -> None:
    from forge.providers.base import ToolCall
    from forge.tools import call_tool

    ctx = make_ctx(tmp_project)
    await call_tool(
        ctx, ToolCall(id="c", name="write_file", arguments={"path": "x.txt", "content": "x"})
    )
    assert (await run_command(ctx, "/undo")) == "undid the last change: x.txt"
    assert not (tmp_project / "x.txt").exists()
    assert await run_command(ctx, "/undo") == "nothing to undo"
