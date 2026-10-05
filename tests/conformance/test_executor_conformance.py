"""Every Executor implementation must run commands and jobs the same way."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge.local.local_executor import LocalExecutor
from forge.ports import Command, Executor, JobNotFoundError, SandboxPolicy
from forge.runtime.shell import find_shell

SHELL = "bash" if find_shell("bash") else "powershell"
POLICY = SandboxPolicy(mode="full-access")


def script(root: Path, text: str, timeout_s: float = 30) -> Command:
    return Command(script=text, shell=SHELL, cwd=str(root), timeout_s=timeout_s)  # type: ignore[arg-type]


@pytest.fixture(params=["local"])
async def executor(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Executor]:
    local = LocalExecutor(tmp_path)
    yield local
    await local.close()


async def test_run_reports_output_and_exit_code(executor: Executor, tmp_path: Path) -> None:
    ok = await executor.run(script(tmp_path, "echo hello"), POLICY)
    assert ok.exit_code == 0 and ok.stdout.strip() == "hello"
    failed = await executor.run(script(tmp_path, "exit 3"), POLICY)
    assert failed.exit_code == 3


async def test_background_job_output_and_stop(executor: Executor, tmp_path: Path) -> None:
    started = await executor.run(
        script(tmp_path, "echo started; sleep 30"), POLICY, background=True
    )
    assert started.exit_code is None and started.job_id is not None
    for _ in range(100):
        output = await executor.job_output(started.job_id)
        if "started" in output.stdout:
            break
        await asyncio.sleep(0.05)
    assert "started" in output.stdout and output.exit_code is None
    stopped = await executor.job_stop(started.job_id)
    assert stopped.job_id == started.job_id
    with pytest.raises(JobNotFoundError):
        await executor.job_output("j999")


@pytest.mark.skipif(SHELL != "bash", reason="uses bash sleep")
async def test_a_long_command_moves_to_the_background(executor: Executor, tmp_path: Path) -> None:
    slow = await executor.run(script(tmp_path, "echo early; sleep 30", timeout_s=1), POLICY)
    assert slow.timed_out and slow.job_id is not None
    await executor.job_stop(slow.job_id)
