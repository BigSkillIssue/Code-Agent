"""repo_map (docs/TOOLS.md: repo_map). Needs the Python grammar, downloaded on first use."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from forge.ctx import Ctx
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.ignore import project_files
from forge.runtime.repomap import build_map, language_of
from forge.tools import call_tool

pytestmark = pytest.mark.skipif(
    language_of(Path("x.py")) is None, reason="the Python tree-sitter grammar could not be loaded"
)

CORE = '''"""Core."""

LIMIT = 10
_hidden = 1


class Engine:
    def start(self, speed: int) -> None:
        pass

    def _reset(self):
        pass


def make_engine() -> Engine:
    return Engine()


def _private_helper():
    pass
'''
USER_A = "from core import Engine, make_engine\n\ndef drive():\n    return make_engine()\n"
USER_B = "from core import Engine\n\ndef park(e: Engine):\n    return e\n"


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


@pytest.fixture
def mapped(ctx: Ctx) -> Ctx:
    (ctx.root / "core.py").write_text(CORE)
    (ctx.root / "a_user.py").write_text(USER_A)
    (ctx.root / "b_user.py").write_text(USER_B)
    (ctx.root / "notes.md").write_text("# notes\n")
    return ctx


async def test_most_referenced_file_comes_first(mapped: Ctx) -> None:
    result = await run(mapped, "repo_map")
    assert result.ok, result.text
    lines = result.text.splitlines()
    assert lines[0] == "core.py"
    assert "│ LIMIT = 10" in lines
    assert "│ class Engine" in lines
    assert "│   def start(self, speed: int) -> None" in lines
    assert "│ def make_engine() -> Engine" in lines
    assert lines[-1] == (
        "[repo_map: 3 of 3 files shown within 2000 tokens; 1 files in unsupported languages]"
    )


async def test_private_names_hidden_by_default(mapped: Ctx) -> None:
    hidden = (await run(mapped, "repo_map")).text
    assert "_private_helper" not in hidden and "_reset" not in hidden and "_hidden" not in hidden
    shown = (await run(mapped, "repo_map", include_private=True)).text
    assert "def _private_helper()" in shown and "def _reset(self)" in shown


async def test_budget_is_respected(ctx: Ctx) -> None:
    for n in range(40):
        body = "".join(
            f"def function_{n}_{i}(argument_one, argument_two):\n    pass\n" for i in range(10)
        )
        (ctx.root / f"mod_{n:02}.py").write_text(body)
    result = await run(ctx, "repo_map", max_tokens=200)
    body = result.text.rsplit("\n", 1)[0]
    assert len(body) <= 200 * 4
    assert "of 40 files shown within 200 tokens" in result.text


async def test_cache_is_reused(mapped: Ctx) -> None:
    files = await project_files(mapped.root)
    first = await asyncio.to_thread(build_map, mapped.root, files)
    second = await asyncio.to_thread(build_map, mapped.root, files)
    assert first.parsed == 3 and second.parsed == 0
    assert (mapped.root / ".forge" / "cache" / "repomap.json").is_file()
    assert [f.rel for f in second.files] == [f.rel for f in first.files]


async def test_bad_arguments(ctx: Ctx) -> None:
    assert (await run(ctx, "repo_map", max_tokens=100)).code == "invalid_args"
    assert (await run(ctx, "repo_map", path="missing")).code == "not_found"
