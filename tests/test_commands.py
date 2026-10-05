"""Slash commands: built-ins, custom commands from .forge/commands, and /init."""

import shutil
from pathlib import Path

import pytest

from forge.commands import handle_command, run_command
from forge.ctx import Ctx
from forge.plan import Plan, Step, TaskSpec
from support import init_repo, make_ctx

BUGGY = Path(__file__).resolve().parents[1] / "examples" / "buggy"


@pytest.fixture
def ctx(tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ctx:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    return make_ctx(tmp_project)


def write_command(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


async def test_custom_command_expands_its_template(ctx: Ctx, tmp_path: Path) -> None:
    write_command(
        ctx.root / ".forge" / "commands",
        "review",
        "---\ndescription: Review code for bugs\n---\n"
        "Review $ARGUMENTS for bugs and report each with path:line.\n",
    )
    write_command(tmp_path / "home" / "commands", "review", "user version $ARGUMENTS")
    write_command(tmp_path / "home" / "commands", "explain", "Explain this project simply.")
    result = await handle_command(ctx, "/review src/")
    assert result.prompt == "Review src/ for bugs and report each with path:line."
    assert (await handle_command(ctx, "/explain")).prompt == "Explain this project simply."
    assert (
        await handle_command(ctx, "/explain the parser")
    ).prompt == "Explain this project simply.\n\nthe parser"
    assert (
        await run_command(ctx, "/review api/")
        == "running: Review api/ for bugs and report each with path:line."
    )
    help_text = await run_command(ctx, "/help")
    assert "/review" in help_text and "Review code for bugs" in help_text and "/init" in help_text


async def test_init_writes_a_sensible_forge_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    root = tmp_path / "buggy"
    shutil.copytree(BUGGY, root)
    init_repo(root)
    ctx = make_ctx(root)
    reply = await run_command(ctx, "/init")
    assert reply.startswith("wrote FORGE.md (")
    text = (root / "FORGE.md").read_text()
    assert "## Commands\n\n- Test: `python -m pytest -q`\n" in text
    assert "## Layout\n\n- `README.md`\n- `calc.py`\n- `test_calc.py`\n" in text
    assert (
        await run_command(ctx, "/init")
        == "FORGE.md already exists; edit it, or delete it and run /init again"
    )
    assert await run_command(ctx, "/undo") == "undid the last change: FORGE.md"


async def test_init_finds_tool_commands(ctx: Ctx) -> None:
    (ctx.root / "package.json").write_text('{"scripts": {"build": "tsc", "test": "vitest"}}')
    (ctx.root / "Makefile").write_text("lint:\n\truff check .\n")
    (ctx.root / "src").mkdir()
    (ctx.root / "src" / "a.ts").write_text("")
    await run_command(ctx, "/init")
    text = (ctx.root / "FORGE.md").read_text()
    assert "- Build: `npm run build`\n- Test: `npm run test`\n- Lint: `make lint`" in text
    assert "- `src/` (1 files)" in text


async def test_go_agents_and_unknown(ctx: Ctx) -> None:
    assert (await handle_command(ctx, "/go")).text == "no plan to continue"
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["x"], size="small")
    ctx.session.plan = Plan(
        spec=spec, steps=[Step(id="s1", title="t", detail="", check="review: ok")]
    )
    go = await handle_command(ctx, "/go")
    assert go.resume and go.text == "continuing the plan"
    agents = await run_command(ctx, "/agents")
    assert agents.splitlines()[0].split()[:3] == ["id", "name", "role"]
    assert (await run_command(ctx, "/nope")).startswith("unknown command /nope")
