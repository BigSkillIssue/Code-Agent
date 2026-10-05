"""Tests for the persistent shells, background jobs and the shell tools."""

import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.shell import find_shell
from forge.tools import call_tool, first_program, shell_succeeded
from support import make_ctx

PYTHON = Path(sys.executable).as_posix()
no_bash = pytest.mark.skipif(find_shell("bash") is None, reason="bash is not installed")
no_pwsh = pytest.mark.skipif(find_shell("powershell") is None, reason="PowerShell is not installed")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@pytest.fixture
async def sh(tmp_project: Path) -> AsyncIterator[Ctx]:
    executor = LocalExecutor(tmp_project.resolve())
    ctx = make_ctx(tmp_project, executor=executor)
    yield ctx
    await executor.close()


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def section(text: str, name: str) -> str:
    """The body of a `--- name ---` section."""
    after = text.split(f"--- {name} ---\n", 1)[1]
    return after.split("\n--- ", 1)[0]


# ---------------------------------------------------------------- bash


@no_bash
async def test_stdout_and_stderr_sections(sh: Ctx) -> None:
    result = await run(sh, "bash", command="echo out; echo err >&2")
    assert result.ok, result.text
    assert result.text.startswith("exit_code: 0\nduration: ")
    assert section(result.text, "stdout") == "out"
    assert section(result.text, "stderr") == "err"


@no_bash
async def test_nonzero_exit_is_an_error(sh: Ctx) -> None:
    result = await run(sh, "bash", command="(exit 2)")
    assert result.code == "exit_nonzero"
    assert result.text.startswith("error[exit_nonzero]: exit code 2\nexit_code: 2")


@no_bash
async def test_grep_without_match_is_ok(sh: Ctx) -> None:
    (sh.root / "f.txt").write_text("hello\n")
    result = await run(sh, "bash", command="grep nothing f.txt")
    assert result.ok and "exit_code: 1" in result.text


@no_bash
async def test_cd_persists_and_leaving_the_root_resets(sh: Ctx) -> None:
    result = await run(sh, "bash", command="mkdir -p sub && cd sub")
    assert "cwd: sub" in result.text
    assert sh.cwd == sh.root / "sub"
    pwd = await run(sh, "bash", command="pwd")
    assert section(pwd.text, "stdout").rstrip().endswith("/sub")
    away = await run(sh, "bash", command="cd /")
    assert away.text.endswith("note: cwd reset to sub")
    assert sh.cwd == sh.root / "sub"


@no_bash
async def test_environment_is_quiet_and_stdin_closed(sh: Ctx) -> None:
    result = await run(sh, "bash", command='echo "$CI $PAGER $NO_COLOR"; cat')
    assert result.ok and section(result.text, "stdout") == "1 cat 1"


@no_bash
async def test_syntax_error_does_not_hang(sh: Ctx) -> None:
    started = time.monotonic()
    result = await run(sh, "bash", command="echo 'unterminated")
    assert result.code == "exit_nonzero"
    assert time.monotonic() - started < 10
    assert (await run(sh, "bash", command="echo still alive")).ok


@no_bash
async def test_exit_restarts_the_shell(sh: Ctx) -> None:
    result = await run(sh, "bash", command="exit 3")
    assert result.text.startswith("error[exit_nonzero]: exit code 3")
    again = await run(sh, "bash", command="echo back")
    assert again.ok and section(again.text, "stdout") == "back"


@no_bash
async def test_timeout_moves_command_to_background(sh: Ctx) -> None:
    script = (
        f"\"{PYTHON}\" -c \"import time; print('early', flush=True); time.sleep(2); print('late')\""
    )
    result = await run(sh, "bash", command=script, timeout_s=1)
    assert result.ok, result.text
    assert result.text.startswith("timeout: still running after 1s, moved to background as job j1")
    done = await run(sh, "job_output", job_id="j1", wait_s=10)
    while "exited with code" not in done.text:
        done = await run(sh, "job_output", job_id="j1", wait_s=1)
    assert done.text.startswith("job j1: exited with code 0")
    assert "late" in done.text
    assert (await run(sh, "bash", command="echo next")).ok  # a fresh shell took over


@no_bash
async def test_sleep_is_killed_instead_of_backgrounded(sh: Ctx) -> None:
    result = await run(sh, "bash", command="sleep 30", timeout_s=1)
    assert result.code == "timeout"


@no_bash
async def test_background_job_log_and_paging(sh: Ctx) -> None:
    script = (
        f'"{PYTHON}" -c "import time\nfor i in range(5): print(i, flush=True); time.sleep(0.05)"'
    )
    started = await run(sh, "bash", command=script, background=True, description="count")
    assert started.text.startswith("started job j1 (pid ")
    assert started.text.splitlines()[0].endswith(": count")
    assert "log: .forge/jobs/j1.log" in started.text
    result = await run(sh, "job_output", job_id="j1", wait_s=5)
    for _ in range(50):
        if "exited with code 0" in result.text:
            break
        time.sleep(0.05)
        result = await run(sh, "job_output", job_id="j1")
    assert "exited with code 0" in result.text
    later = await run(sh, "job_output", job_id="j1", since_line=3)
    assert later.text.splitlines()[1:] == [
        "lines 4-5 of 5; next since_line: 5",
        "--- output ---",
        "3",
        "4",
    ]
    empty = await run(sh, "job_output", job_id="j1", since_line=5)
    assert empty.text.endswith("--- output ---\n(no new output)")


@no_bash
async def test_unknown_job_lists_known_jobs(sh: Ctx) -> None:
    await run(sh, "bash", command="echo hi", background=True)
    result = await run(sh, "job_output", job_id="j9")
    assert result.code == "not_found" and "hint: known jobs: j1" in result.text
    assert (await run(sh, "job_output", job_id="x1")).code == "invalid_args"


@no_bash
async def test_stop_a_running_job(sh: Ctx) -> None:
    await run(sh, "bash", command="sleep 60", background=True)
    started = time.monotonic()
    result = await run(sh, "job_stop", job_id="j1")
    assert result.ok and result.text.startswith("job j1 stopped (")
    assert time.monotonic() - started < 6
    status = await run(sh, "job_output", job_id="j1")
    assert "exited with code" in status.text
    again = await run(sh, "job_stop", job_id="j1")
    assert again.text.startswith("job j1 had already exited with code")


@no_bash
@posix_only
async def test_stopping_a_job_stops_its_children(sh: Ctx) -> None:
    await run(sh, "bash", command="sleep 60 & echo $! > child.pid; wait", background=True)
    pid_file = sh.root / "child.pid"
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.02)
    child = int(pid_file.read_text())
    await run(sh, "job_stop", job_id="j1")
    time.sleep(0.2)
    assert not is_running(child)


def is_running(pid: int) -> bool:
    """True if the process exists and is not a zombie (containers may never reap orphans)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    proc = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return proc.stdout.strip()[:1] not in ("", "Z")


def test_first_program_and_benign_exit_codes() -> None:
    assert first_program("FOO=1 /usr/bin/grep -r x .") == "grep"
    assert first_program("git diff --stat") == "git-diff"
    assert shell_succeeded("bash", "git diff --exit-code", 1)
    assert not shell_succeeded("bash", "pytest", 1)
    assert shell_succeeded("powershell", "robocopy a b", 3)
    assert not shell_succeeded("powershell", "robocopy a b", 8)


# ---------------------------------------------------------------- PowerShell


@no_pwsh
async def test_powershell_streams(sh: Ctx) -> None:
    result = await run(sh, "powershell", command="Write-Output 'out'; Write-Error 'bad'")
    assert section(result.text, "stdout") == "out"
    assert "bad" in section(result.text, "stderr")


@no_pwsh
async def test_powershell_exit_code(sh: Ctx) -> None:
    result = await run(sh, "powershell", command="exit 3")
    assert result.text.startswith("error[exit_nonzero]: exit code 3")
    assert (await run(sh, "powershell", command="Write-Output ok")).ok


@no_pwsh
async def test_powershell_location_and_utf8(sh: Ctx) -> None:
    (sh.root / "sub").mkdir()
    moved = await run(sh, "powershell", command="Set-Location sub")
    assert "cwd: sub" in moved.text
    result = await run(sh, "powershell", command="Write-Output 'Grüße ✓'; (Get-Location).Path")
    out = section(result.text, "stdout").splitlines()
    assert out[0] == "Grüße ✓"
    assert out[1].replace("\\", "/").endswith("/sub")


@no_pwsh
async def test_powershell_unknown_command_fails(sh: Ctx) -> None:
    result = await run(sh, "powershell", command="no-such-command-xyz")
    assert result.code == "exit_nonzero" and "no-such-command-xyz" in result.text


@pytest.mark.skipif(sys.platform != "win32", reason="robocopy is Windows-only")
async def test_robocopy_exit_one_is_ok(sh: Ctx) -> None:
    (sh.root / "a").mkdir()
    (sh.root / "a" / "f.txt").write_text("x")
    result = await run(sh, "powershell", command="robocopy a b f.txt")
    assert result.ok, result.text
