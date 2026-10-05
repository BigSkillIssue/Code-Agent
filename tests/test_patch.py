"""apply_patch and the patch format (docs/TOOLS.md: apply_patch)."""

from pathlib import Path
from typing import Any

import pytest

from forge.ctx import Ctx
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.patch import PatchSyntaxError, parse_patch
from forge.tools import call_tool


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def put_read(ctx: Ctx, rel: str, content: str) -> Path:
    path = ctx.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    assert (await run(ctx, "read_file", path=rel)).ok
    return path


def patch(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


async def test_add_update_delete_and_move_in_one_patch(ctx: Ctx) -> None:
    app = await put_read(ctx, "src/app.py", "import os\n\ndef main():\n    return 1\n")
    old = await put_read(ctx, "src/old.py", "x = 1\n")
    legacy = await put_read(ctx, "legacy/x.py", "gone\n")
    text = patch(
        "*** Update File: src/app.py",
        "@@ def main():",
        "-    return 1",
        "+    return 2",
        "*** Add File: src/util.py",
        "+def helper():",
        "+    return 3",
        "*** Update File: src/old.py",
        "*** Move to: src/new.py",
        "-x = 1",
        "+x = 2",
        "*** Delete File: legacy/x.py",
    )
    result = await run(ctx, "apply_patch", patch=text)
    assert result.ok, result.text
    assert result.text.splitlines() == [
        "applied patch: 4 files",
        "  M src/app.py (+1 -1)",
        "  A src/util.py (+2)",
        "  R src/old.py -> src/new.py (+1 -1)",
        "  D legacy/x.py",
    ]
    assert app.read_text() == "import os\n\ndef main():\n    return 2\n"
    assert (ctx.root / "src/util.py").read_text() == "def helper():\n    return 3\n"
    assert not old.exists() and (ctx.root / "src/new.py").read_text() == "x = 2\n"
    assert not legacy.exists()


async def test_failing_second_hunk_changes_nothing(ctx: Ctx) -> None:
    a = await put_read(ctx, "a.py", "one\ntwo\nthree\n")
    text = patch(
        "*** Update File: a.py",
        "@@",
        "-one",
        "+ONE",
        "@@",
        "-four",
        "+FOUR",
        "*** Add File: b.py",
        "+new",
    )
    result = await run(ctx, "apply_patch", patch=text)
    assert not result.ok and result.code == "no_match"
    assert "a.py hunk 2" in result.text and "four" in result.text
    assert a.read_text() == "one\ntwo\nthree\n"
    assert not (ctx.root / "b.py").exists()


async def test_whitespace_tolerant_matching(ctx: Ctx) -> None:
    path = await put_read(ctx, "w.py", "def f():   \n    return 1\n")
    text = patch("*** Update File: w.py", "@@", " def f():", "-  return 1", "+    return 5")
    result = await run(ctx, "apply_patch", patch=text)
    assert result.ok, result.text
    assert path.read_text() == "def f():   \n    return 5\n"


async def test_header_narrows_the_search(ctx: Ctx) -> None:
    path = await put_read(ctx, "h.py", "def a():\n    return 0\n\ndef b():\n    return 0\n")
    text = patch("*** Update File: h.py", "@@ def b():", "-    return 0", "+    return 9")
    result = await run(ctx, "apply_patch", patch=text)
    assert result.ok, result.text
    assert path.read_text() == "def a():\n    return 0\n\ndef b():\n    return 9\n"


async def test_ambiguous_hunk_is_not_unique(ctx: Ctx) -> None:
    await put_read(ctx, "d.py", "x\nx\n")
    result = await run(ctx, "apply_patch", patch=patch("*** Update File: d.py", "-x", "+y"))
    assert result.code == "not_unique"


async def test_add_on_existing_file_fails(ctx: Ctx) -> None:
    await put_read(ctx, "e.py", "x\n")
    result = await run(ctx, "apply_patch", patch=patch("*** Add File: e.py", "+y"))
    assert not result.ok and result.code == "invalid_args" and "already exists" in result.text


async def test_update_needs_a_read_first(ctx: Ctx) -> None:
    (ctx.root / "u.py").write_text("x\n")
    result = await run(ctx, "apply_patch", patch=patch("*** Update File: u.py", "-x", "+y"))
    assert result.code == "not_read"


async def test_parse_error_names_the_line(ctx: Ctx) -> None:
    text = patch("*** Add File: n.py", "+ok", "oops")
    result = await run(ctx, "apply_patch", patch=text)
    assert result.code == "invalid_args" and "line 4" in result.text
    with pytest.raises(PatchSyntaxError, match="line 1"):
        parse_patch("hello\n*** End Patch")


async def test_crlf_files_keep_their_line_endings(ctx: Ctx) -> None:
    path = await put_read(ctx, "c.txt", "a\r\nb\r\n")
    result = await run(ctx, "apply_patch", patch=patch("*** Update File: c.txt", " a", "-b", "+c"))
    assert result.ok, result.text
    assert path.read_bytes() == b"a\r\nc\r\n"


async def test_protected_path_is_refused(ctx: Ctx) -> None:
    result = await run(ctx, "apply_patch", patch=patch("*** Add File: .git/x", "+y"))
    assert result.code == "protected_path"
