"""Agent worktrees and merging them back (runtime/worktree.py, team merge flow)."""

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.plan import Plan, Step, TaskSpec
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime.worktree import (
    WorktreeError,
    apply_merge,
    create_worktree,
    prepare_merge,
    remove_worktree,
)
from forge.team import AgentRegistry
from forge.tools import call_tool
from forge.wiring import close_session
from support import git, make_ctx


@pytest.fixture
def repo(tmp_project: Path) -> Path:
    (tmp_project / "a.py").write_text("A = 1\n")
    (tmp_project / "b.py").write_text("B = 1\n")
    git(tmp_project, "add", "-A")
    git(tmp_project, "commit", "-q", "-m", "start")
    return tmp_project


async def test_parallel_edits_to_different_files_merge_cleanly(repo: Path) -> None:
    (repo / "notes.txt").write_text("uncommitted\n")  # the user's work in progress
    first = await create_worktree(repo, "s1", "a1")
    second = await create_worktree(repo, "s1", "a2")
    assert (first.path / "notes.txt").read_text() == "uncommitted\n"  # starts from the live tree
    (first.path / "a.py").write_text("A = 2\n")
    (second.path / "b.py").write_text("B = 2\n")
    (second.path / "new.py").write_text("NEW = 1\n")
    for tree in (first, second):
        prepared = await prepare_merge(repo, tree, "work")
        assert prepared.ok
        await apply_merge(repo, tree, prepared)
    assert (repo / "a.py").read_text() == "A = 2\n"
    assert (repo / "b.py").read_text() == "B = 2\n" and (repo / "new.py").read_text() == "NEW = 1\n"
    assert (repo / "notes.txt").read_text() == "uncommitted\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""  # the user's index is untouched
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    for tree in (first, second):
        await remove_worktree(repo, tree)
    assert not first.path.exists()


async def test_conflict_is_reported_not_resolved(repo: Path) -> None:
    tree = await create_worktree(repo, "s1", "a1")
    (tree.path / "a.py").write_text("A = 'agent'\n")
    (repo / "a.py").write_text("A = 'user'\n")
    prepared = await prepare_merge(repo, tree, "work")
    assert not prepared.ok and prepared.conflicts == ["a.py"]
    assert (repo / "a.py").read_text() == "A = 'user'\n"


def team_ctx(root: Path, roles: dict[str, list[FakeTurn]]) -> Ctx:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(roles={role: [f"fake/{role}"] for role in roles})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg)


def call(tool: str, **arguments: Any) -> FakeToolCall:
    return FakeToolCall(name=tool, arguments=arguments)


async def test_worktree_agent_edits_are_merged_after_it_finishes(repo: Path) -> None:
    ctx = team_ctx(
        repo,
        {
            "coder": [
                FakeTurn(
                    tool_calls=[
                        call("spawn_agent", role="tester", task="add tests", isolation="worktree")
                    ]
                ),
                FakeTurn(text="ok"),
            ],
            "tester": [
                FakeTurn(
                    tool_calls=[
                        call("write_file", path="test_a.py", content="def test_a():\n    pass\n")
                    ]
                ),
                FakeTurn(text="added test_a.py"),
            ],
        },
    )
    result = await run_agent(ctx, "tests please")
    report = result.messages[2].tool_result
    assert report is not None and report.ok
    assert report.text.splitlines()[-1] == "merged into the main tree: 1 files (test_a.py)"
    assert (repo / "test_a.py").read_text() == "def test_a():\n    pass\n"
    team = ctx.state.team
    assert isinstance(team, AgentRegistry)
    worktree = team.agents["a1"].tree
    assert worktree is not None and worktree.path.exists()
    await close_session(ctx)
    assert not worktree.path.exists()


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def test_board_task_conflict_goes_back_to_its_owner(repo: Path) -> None:
    ctx = team_ctx(repo, {"reviewer": [FakeTurn(text='{"pass": true, "reason": "ok"}')] * 3})
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["ok"], size="large")
    ctx.session.plan = Plan(
        spec=spec, steps=[Step(id="s1", title="Change A", detail="", check="review: done")]
    )
    ctx.state.team_mode = True
    team = ctx.state.team
    assert isinstance(team, AgentRegistry)
    info = team.add("coder", "change A", None, "main")
    team.main_root = repo
    child = await team.isolate(ctx, info, replace(ctx, agent_id="a1"))
    assert (await run(child, "claim_task", task_id="s1")).ok
    (child.root / "a.py").write_text("A = 'agent'\n")
    (repo / "a.py").write_text("A = 'lead'\n")
    result = await run(child, "update_task", task_id="s1", status="done", result="changed A")
    assert result.code == "check_failed" and "merge conflict in a.py" in result.text
    assert "git merge forge/test-session/main" in result.text
    assert ctx.session.plan.steps[0].status == "doing"
    assert team.take_messages("main") == []  # the lead hears nothing until it merges
    assert (repo / "a.py").read_text() == "A = 'lead'\n"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin rights on Windows")
async def test_a_merge_never_writes_through_an_ignored_link(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".gitignore").write_text("build\n")
    os.symlink(outside, repo / "build", target_is_directory=True)  # planted by a command
    tree = await create_worktree(repo, "s1", "a1")
    (tree.path / "build").mkdir()
    (tree.path / "build" / "pwned.txt").write_text("x\n")
    git(tree.path, "add", "-f", "build/pwned.txt")
    prepared = await prepare_merge(repo, tree, "work")
    with pytest.raises(WorktreeError, match="outside"):
        await apply_merge(repo, tree, prepared)
    assert list(outside.iterdir()) == []


async def test_a_merge_never_writes_forge_settings(repo: Path) -> None:
    tree = await create_worktree(repo, "s1", "a1")
    (tree.path / ".forge").mkdir(exist_ok=True)
    (tree.path / ".forge" / "config.toml").write_text("[hooks]\n")
    git(tree.path, "add", "-f", ".forge/config.toml")
    (tree.path / "a.py").write_text("A = 2\n")
    prepared = await prepare_merge(repo, tree, "work")
    with pytest.raises(WorktreeError, match="protected"):
        await apply_merge(repo, tree, prepared)
    assert not (repo / ".forge" / "config.toml").exists()
    assert (repo / "a.py").read_text() == "A = 1\n"  # all or nothing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_a_merge_keeps_the_executable_bit(repo: Path) -> None:
    tree = await create_worktree(repo, "s1", "a1")
    script = tree.path / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    prepared = await prepare_merge(repo, tree, "work")
    await apply_merge(repo, tree, prepared)
    assert os.access(repo / "run.sh", os.X_OK)
