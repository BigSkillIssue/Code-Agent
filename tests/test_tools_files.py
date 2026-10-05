"""Tests for the core file and search tools (docs/TOOLS.md: Files and Search)."""

import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.providers.base import Capabilities, ToolCall, ToolResult
from forge.providers.fake import FakeProvider
from forge.providers.registry import register_provider
from forge.runtime.search import GrepQuery, format_hits, search
from forge.tools import call_tool
from support import make_ctx


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def put(root: Path, rel: str, content: str | bytes, mtime: int | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, ns=(mtime * 10**9, mtime * 10**9))
    return path


# ---------------------------------------------------------------- read_file


async def test_read_numbers_lines_and_records_full_read(ctx: Ctx) -> None:
    path = put(ctx.root, "f.txt", "a\nb\nc\n")
    result = await run(ctx, "read_file", path="f.txt")
    assert result.ok
    assert result.text == "file: f.txt (3 lines, 6 B)\n     1\ta\n     2\tb\n     3\tc"
    entry = ctx.ledger.get(path)
    assert entry is not None and entry.full


async def test_read_offset_limit_and_partial_note(ctx: Ctx) -> None:
    path = put(ctx.root, "f.txt", "".join(f"line {i}\n" for i in range(1, 11)))
    result = await run(ctx, "read_file", path="f.txt", offset=3, limit=2)
    lines = result.text.splitlines()
    assert lines[1:3] == ["     3\tline 3", "     4\tline 4"]
    assert lines[-1] == '[PARTIAL: lines 3-4 of 10. Continue with read_file("f.txt", offset=5).]'
    entry = ctx.ledger.get(path)
    assert entry is not None and not entry.full


async def test_read_cuts_long_lines(ctx: Ctx) -> None:
    put(ctx.root, "long.txt", "x" * 2500 + "\n")
    result = await run(ctx, "read_file", path="long.txt")
    assert result.text.endswith("x" * 2000 + " [line truncated]")


async def test_read_empty_file(ctx: Ctx) -> None:
    put(ctx.root, "empty.py", "")
    assert (await run(ctx, "read_file", path="empty.py")).text == "file: empty.py (empty)"


async def test_read_crlf_without_carriage_returns(ctx: Ctx) -> None:
    put(ctx.root, "win.txt", b"one\r\ntwo\r\n")
    result = await run(ctx, "read_file", path="win.txt")
    assert "\r" not in result.text
    assert result.text.endswith("     1\tone\n     2\ttwo")


async def test_read_latin1_adds_note(ctx: Ctx) -> None:
    put(ctx.root, "old.txt", "caf\xe9\n".encode("latin-1"))
    result = await run(ctx, "read_file", path="old.txt")
    assert "note: file is not valid UTF-8, read as Latin-1" in result.text
    assert "café" in result.text


async def test_read_missing_file_suggests_close_names(ctx: Ctx) -> None:
    put(ctx.root, "config.py", "x = 1\n")
    result = await run(ctx, "read_file", path="confg.py")
    assert result.code == "not_found"
    assert "hint: did you mean config.py" in result.text


async def test_read_folder_points_to_list_dir(ctx: Ctx) -> None:
    (ctx.root / "src").mkdir()
    result = await run(ctx, "read_file", path="src")
    assert result.code == "invalid_args" and "use list_dir" in result.text


async def test_read_offset_past_end(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "a\nb\n")
    result = await run(ctx, "read_file", path="f.txt", offset=9)
    assert result.text == "error[invalid_args]: offset 9 is past the end; file has 2 lines"


async def test_read_binary_file(ctx: Ctx) -> None:
    put(ctx.root, "blob.bin", b"\x00\x01\x02")
    assert (await run(ctx, "read_file", path="blob.bin")).code == "binary_file"


def tiny_png(width: int = 3, height: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0\0\0\0"


@pytest.mark.parametrize("vision", [True, False])
async def test_read_image_depends_on_vision(tmp_project: Path, vision: bool) -> None:
    cfg = ForgeConfig(roles={"coder": ["fake/model"]})
    register_provider(cfg, FakeProvider(caps=Capabilities(vision=vision)))
    ctx = make_ctx(tmp_project, cfg=cfg)
    put(ctx.root, "pic.png", tiny_png())
    result = await run(ctx, "read_file", path="pic.png")
    if vision:
        assert result.ok and result.text.startswith("image: pic.png (3x2, ")
        assert result.images[0].media_type == "image/png"
    else:
        assert result.code == "unsupported"


async def test_read_notebook_cells(ctx: Ctx) -> None:
    notebook = {
        "cells": [
            {"cell_type": "markdown", "id": "m1", "source": ["# Title"]},
            {
                "cell_type": "code",
                "id": "c1",
                "source": "print(1)",
                "outputs": [{"output_type": "stream", "text": ["1\n"]}],
            },
        ]
    }
    put(ctx.root, "nb.ipynb", json.dumps(notebook))
    text = (await run(ctx, "read_file", path="nb.ipynb")).text
    assert "--- cell 0 [markdown] id=m1 ---\n# Title" in text
    assert "--- cell 1 [code] id=c1 ---\nprint(1)\n--- output ---\n1" in text


def tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
async def test_read_pdf_text(ctx: Ctx) -> None:
    put(ctx.root, "doc.pdf", tiny_pdf("Hello PDF"))
    result = await run(ctx, "read_file", path="doc.pdf")
    assert result.ok and "Hello PDF" in result.text


async def test_pages_only_for_pdf(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "a\n")
    assert (await run(ctx, "read_file", path="f.txt", pages="1")).code == "invalid_args"


# ---------------------------------------------------------------- write_file


async def test_write_creates_parents(ctx: Ctx) -> None:
    result = await run(ctx, "write_file", path="a/b/c.txt", content="one\ntwo\n")
    assert result.text == "created a/b/c.txt (2 lines)"
    assert (ctx.root / "a" / "b" / "c.txt").read_text() == "one\ntwo\n"


async def test_overwrite_without_read_fails(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "old\n")
    result = await run(ctx, "write_file", path="f.txt", content="new\n")
    assert result.code == "not_read"
    assert (ctx.root / "f.txt").read_text() == "old\n"


async def test_overwrite_after_partial_read_fails(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "a\nb\nc\n")
    await run(ctx, "read_file", path="f.txt", limit=1)
    assert (await run(ctx, "write_file", path="f.txt", content="x\n")).code == "not_read"


async def test_overwrite_after_external_change_is_stale(ctx: Ctx) -> None:
    path = put(ctx.root, "f.txt", "old\n")
    await run(ctx, "read_file", path="f.txt")
    path.write_text("changed by someone else\n")
    result = await run(ctx, "write_file", path="f.txt", content="new\n")
    assert result.code == "stale" and "hint: read the file again" in result.text


async def test_overwrite_reports_diff_counts(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "a\nb\nc\n")
    await run(ctx, "read_file", path="f.txt")
    result = await run(ctx, "write_file", path="f.txt", content="a\nB\nc\nd\n")
    assert result.text == "overwrote f.txt (3 -> 4 lines, +2 -1)"


async def test_overwrite_keeps_crlf_and_trailing_newline(ctx: Ctx) -> None:
    path = put(ctx.root, "win.txt", b"one\r\ntwo\r\n")
    await run(ctx, "read_file", path="win.txt")
    await run(ctx, "write_file", path="win.txt", content="uno\ndos")
    assert path.read_bytes() == b"uno\r\ndos\r\n"


async def test_write_refuses_git_and_outside_root(ctx: Ctx, tmp_path: Path) -> None:
    (ctx.root / ".git").mkdir(exist_ok=True)
    result = await run(ctx, "write_file", path=".git/config", content="x")
    assert result.code == "protected_path"
    outside = tmp_path / "elsewhere.txt"
    result = await run(ctx, "write_file", path=str(outside), content="x")
    assert result.code == "outside_root" and not outside.exists()


async def test_write_then_overwrite_without_reading_again(ctx: Ctx) -> None:
    await run(ctx, "write_file", path="new.txt", content="v1\n")
    result = await run(ctx, "write_file", path="new.txt", content="v2\n")
    assert result.ok, result.text  # the model wrote it, so it knows the content


# ---------------------------------------------------------------- edit_file


async def read_then(ctx: Ctx, rel: str, content: str | bytes) -> Path:
    path = put(ctx.root, rel, content)
    await run(ctx, "read_file", path=rel)
    return path


async def test_edit_single_replacement_shows_context(ctx: Ctx) -> None:
    lines = [f"line {i}" for i in range(1, 11)]
    path = await read_then(ctx, "f.txt", "\n".join(lines) + "\n")
    result = await run(ctx, "edit_file", path="f.txt", old="line 5", new="LINE FIVE")
    assert result.text.splitlines() == [
        "edited f.txt: 1 replacement at line 5",
        "     2\tline 2",
        "     3\tline 3",
        "     4\tline 4",
        "     5\tLINE FIVE",
        "     6\tline 6",
        "     7\tline 7",
        "     8\tline 8",
    ]
    assert "LINE FIVE\n" in path.read_text()


async def test_edit_not_unique_lists_lines(ctx: Ctx) -> None:
    await read_then(ctx, "f.txt", "x = 1\ny = 2\nx = 1\n")
    result = await run(ctx, "edit_file", path="f.txt", old="x = 1", new="x = 3")
    assert result.code == "not_unique"
    assert "found at lines 1, 3" in result.text


async def test_edit_replace_all(ctx: Ctx) -> None:
    path = await read_then(ctx, "f.txt", "x = 1\ny = 2\nx = 1\n")
    result = await run(ctx, "edit_file", path="f.txt", old="x = 1", new="x = 3", replace_all=True)
    assert result.text.startswith("edited f.txt: 2 replacements at lines 1, 3")
    assert path.read_text() == "x = 3\ny = 2\nx = 3\n"


async def test_edit_no_match_shows_nearest_lines(ctx: Ctx) -> None:
    await read_then(ctx, "f.py", "def main():\n    return 1\n\ndef helper():\n    pass\n")
    result = await run(ctx, "edit_file", path="f.py", old="def mian():", new="def main2():")
    assert result.code == "no_match"
    assert "     1\tdef main():" in result.text


async def test_edit_with_empty_new_deletes(ctx: Ctx) -> None:
    path = await read_then(ctx, "f.txt", "keep\ndrop me\nkeep\n")
    await run(ctx, "edit_file", path="f.txt", old="drop me\n", new="")
    assert path.read_text() == "keep\nkeep\n"


async def test_edit_crlf_file_with_lf_input(ctx: Ctx) -> None:
    path = await read_then(ctx, "win.txt", b"a\r\nb\r\nc\r\n")
    result = await run(ctx, "edit_file", path="win.txt", old="a\nb", new="A\nB")
    assert result.ok, result.text
    assert path.read_bytes() == b"A\r\nB\r\nc\r\n"


async def test_edit_after_external_change_still_works(ctx: Ctx) -> None:
    path = await read_then(ctx, "f.txt", "alpha\nbeta\n")
    path.write_text("alpha\nbeta\ngamma\n")
    result = await run(ctx, "edit_file", path="f.txt", old="beta", new="BETA")
    assert result.ok and "note: file had changed on disk" in result.text
    assert path.read_text() == "alpha\nBETA\ngamma\n"


async def test_edit_requires_read(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "a\n")
    result = await run(ctx, "edit_file", path="f.txt", old="a", new="b")
    assert result.code == "not_read"


async def test_edit_missing_file_suggests_write_file(ctx: Ctx) -> None:
    result = await run(ctx, "edit_file", path="nope.txt", old="a", new="b")
    assert result.code == "not_found" and "write_file" in result.text


async def test_edit_journal_allows_undo(ctx: Ctx) -> None:
    from forge.runtime.files import journal_dir, undo_last_change

    path = await read_then(ctx, "f.txt", "before\n")
    await run(ctx, "edit_file", path="f.txt", old="before", new="after")
    assert undo_last_change(journal_dir(ctx)) == [path]
    assert path.read_text() == "before\n"


# ---------------------------------------------------------------- list_dir


async def test_list_dir_tree_and_ignores(ctx: Ctx) -> None:
    put(ctx.root, ".gitignore", "secret.txt\n")
    put(ctx.root, "secret.txt", "x")
    put(ctx.root, "src/forge/agent.py", "a" * 4300)
    put(ctx.root, "src/forge/tools.py", "b" * 10)
    put(ctx.root, "src/main.py", "c" * 312)
    put(ctx.root, "README.md", "hi")
    text = (await run(ctx, "list_dir", path=".")).text
    assert text.splitlines() == [
        "./ (2 levels)",
        "├── src/",
        "│   ├── forge/",
        "│   └── main.py  312 B",
        "└── README.md  2 B",
    ]
    shown = (await run(ctx, "list_dir", path=".", depth=3, show_hidden=True)).text
    assert "secret.txt" in shown and ".gitignore" in shown and "agent.py  4.2 KB" in shown


async def test_list_dir_cuts_at_500_entries(ctx: Ctx) -> None:
    for i in range(520):
        put(ctx.root, f"many/f{i:03}.txt", "")
    text = (await run(ctx, "list_dir", path="many", depth=1)).text
    lines = text.splitlines()
    assert len(lines) == 502
    assert lines[-1] == "[cut: 20 more entries; use a deeper path or a lower depth]"


async def test_list_dir_rejects_files_and_bad_depth(ctx: Ctx) -> None:
    put(ctx.root, "f.txt", "")
    assert (await run(ctx, "list_dir", path="f.txt")).code == "invalid_args"
    assert (await run(ctx, "list_dir", depth=9)).code == "invalid_args"


# ---------------------------------------------------------------- glob


async def test_glob_recursive_newest_first(ctx: Ctx) -> None:
    put(ctx.root, "a.py", "", mtime=1000)
    put(ctx.root, "pkg/b.py", "", mtime=3000)
    put(ctx.root, "pkg/deep/c.py", "", mtime=2000)
    put(ctx.root, "notes.md", "", mtime=4000)
    text = (await run(ctx, "glob", pattern="**/*.py")).text
    assert text.splitlines() == ["pkg/b.py", "pkg/deep/c.py", "a.py"]
    assert (await run(ctx, "glob", pattern="*.py")).text == "a.py"


async def test_glob_braces_and_ignored(ctx: Ctx) -> None:
    put(ctx.root, ".gitignore", "gen/\n")
    put(ctx.root, "src/x.ts", "", mtime=100)
    put(ctx.root, "src/y.tsx", "", mtime=200)
    put(ctx.root, "gen/z.ts", "", mtime=300)
    assert (await run(ctx, "glob", pattern="**/*.{ts,tsx}")).text == "src/y.tsx\nsrc/x.ts"
    with_ignored = await run(ctx, "glob", pattern="**/*.ts", include_ignored=True)
    assert with_ignored.text.splitlines()[0] == "gen/z.ts"


async def test_glob_truncates_and_reports_no_match(ctx: Ctx) -> None:
    for i in range(5):
        put(ctx.root, f"f{i}.txt", "", mtime=100 + i)
    text = (await run(ctx, "glob", pattern="*.txt", limit=2)).text
    assert text.splitlines() == ["f4.txt", "f3.txt", "[truncated: 5 matches, showing the 2 newest]"]
    result = await run(ctx, "glob", pattern="*.rs")
    assert result.ok and result.text == 'no files match "*.rs" in .'


async def test_glob_finds_dot_folders_when_named(ctx: Ctx) -> None:
    put(ctx.root, ".github/workflows/ci.yml", "")
    assert (await run(ctx, "glob", pattern="**/*.yml")).text.startswith("no files match")
    assert (await run(ctx, "glob", pattern=".github/**/*.yml")).text == ".github/workflows/ci.yml"


# ---------------------------------------------------------------- grep


@pytest.fixture
def code_tree(ctx: Ctx) -> Path:
    put(ctx.root, ".gitignore", "build/\n")
    put(
        ctx.root,
        "src/auth.py",
        "import os\n\ndef check_token(token):\n"
        "    if not token:\n        return False\n    return True\n",
        mtime=3000,
    )
    put(
        ctx.root,
        "src/api/login.py",
        "from auth import check_token\n\ncheck_token('x')\n",
        mtime=2000,
    )
    put(ctx.root, "README.md", "Token docs\n", mtime=1000)
    put(ctx.root, "build/out.py", "check_token = None\n", mtime=4000)
    return ctx.root


async def test_grep_files_mode(ctx: Ctx, code_tree: Path) -> None:
    text = (await run(ctx, "grep", pattern="check_token")).text
    assert text.splitlines() == ["src/auth.py", "src/api/login.py"]  # build/ is gitignored


async def test_grep_content_mode(ctx: Ctx, code_tree: Path) -> None:
    text = (await run(ctx, "grep", pattern="def check_token", mode="content")).text
    assert text == "src/auth.py:3:def check_token(token):"


async def test_grep_count_mode(ctx: Ctx, code_tree: Path) -> None:
    text = (await run(ctx, "grep", pattern="check_token", mode="count")).text
    assert text.splitlines() == [
        "src/auth.py:1",
        "src/api/login.py:2",
        "total: 3 matches in 2 files",
    ]


async def test_grep_context_and_separators(ctx: Ctx, code_tree: Path) -> None:
    text = (await run(ctx, "grep", pattern="check_token", mode="content", context=1)).text
    assert text.splitlines() == [
        "src/auth.py-2-",
        "src/auth.py:3:def check_token(token):",
        "src/auth.py-4-    if not token:",
        "--",
        "src/api/login.py:1:from auth import check_token",
        "src/api/login.py-2-",
        "src/api/login.py:3:check_token('x')",
    ]


async def test_grep_case_type_and_glob_filters(ctx: Ctx, code_tree: Path) -> None:
    assert (await run(ctx, "grep", pattern="token docs", case_insensitive=True)).text == "README.md"
    assert (await run(ctx, "grep", pattern="oken", type="md")).text == "README.md"
    assert (await run(ctx, "grep", pattern="check", glob="login.py")).text == "src/api/login.py"


async def test_grep_invalid_regex_and_no_match(ctx: Ctx, code_tree: Path) -> None:
    bad = await run(ctx, "grep", pattern="(unclosed")
    assert bad.code == "invalid_args"
    none = await run(ctx, "grep", pattern="nothing-like-this")
    assert none.ok and none.text == "no matches for /nothing-like-this/ in ."


async def test_grep_paging(ctx: Ctx, code_tree: Path) -> None:
    first = (await run(ctx, "grep", pattern="check_token|token", mode="content", head_limit=2)).text
    lines = first.splitlines()
    assert len(lines) == 3 and lines[-1].startswith("[showing 2 of ")
    assert lines[-1].endswith("use offset=2 for more]")
    rest = (await run(ctx, "grep", pattern="check_token|token", mode="content", offset=2)).text
    assert rest.splitlines()[0] not in lines


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
@pytest.mark.parametrize(
    ("mode", "context", "multiline", "pattern"),
    [
        ("files", 0, False, "token"),
        ("content", 0, False, "token"),
        ("content", 1, False, "return"),
        ("count", 0, False, "check_token"),
        ("content", 0, True, r"if not token:\s+return False"),
    ],
)
async def test_python_fallback_matches_ripgrep(
    ctx: Ctx, code_tree: Path, mode: Any, context: int, multiline: bool, pattern: str
) -> None:
    query = GrepQuery(pattern, ctx.root, mode=mode, context=context, multiline=multiline)
    with_rg = format_hits(await search(query, ctx.root, use_rg=True), query, ctx.root)
    with_python = format_hits(await search(query, ctx.root, use_rg=False), query, ctx.root)
    assert with_python == with_rg
