"""A SWE-bench Lite runner: real GitHub issues, judged by the instance's own failing tests.

Each instance is one JSON line from the SWE-bench Lite dataset (`instance_id`, `repo`,
`base_commit`, `problem_statement`, `test_patch`, `FAIL_TO_PASS`). The runner clones the
repo at `base_commit`, lets Forge work on the problem statement, applies the hidden test
patch, and runs the FAIL_TO_PASS tests with pytest in the current Python environment.

Limitation: the official harness builds a per-repo environment (Docker). This runner uses
the current interpreter, so it fits pure-Python repos whose test dependencies are installed.
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

from forge.config import ForgeConfig
from forge.evals import EvalResult, EvalTask, FakeSolution, fake_script
from forge.local.auto_renderer import AutoRenderer
from forge.local.local_executor import LocalExecutor
from forge.local.memory_store import MemoryStore
from forge.pipeline import run_task
from forge.providers.fake import FakeProvider
from forge.runtime.proc import run_argv
from forge.wiring import close_session, install_fake, open_session

CLONE_TIMEOUT_S = 600
TEST_TIMEOUT_S = 900


class SweInstance(BaseModel):
    """One SWE-bench (Lite) task."""

    instance_id: str
    repo: str  # "owner/name" on GitHub, or a local path (tests)
    base_commit: str
    problem_statement: str
    test_patch: str
    FAIL_TO_PASS: list[str]
    forge_fake: FakeSolution | None = None  # offline runs only: what the scripted model does

    @field_validator("FAIL_TO_PASS", mode="before")
    @classmethod
    def json_list(cls, value: Any) -> Any:
        """The dataset stores the test list as a JSON string."""
        return json.loads(value) if isinstance(value, str) else value


def load_instances(path: Path, limit: int | None = None) -> list[SweInstance]:
    """Instances from a JSONL file (one per line)."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [SweInstance.model_validate_json(line) for line in lines[:limit]]


def clone_url(repo: str) -> str:
    """A local path as is, otherwise the GitHub URL."""
    return repo if Path(repo).exists() else f"https://github.com/{repo}.git"


async def git(cwd: Path, *args: str, stdin_text: str | None = None) -> None:
    """Run git; raise RuntimeError with its message on failure."""
    if stdin_text is not None:
        patch = cwd.parent / "test.patch"
        patch.write_text(stdin_text, encoding="utf-8")
        args = (*args, str(patch))
    result = await run_argv(["git", *args], cwd, timeout_s=CLONE_TIMEOUT_S)
    if result.code != 0:
        raise RuntimeError(
            f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[-300:]}"
        )


def fake_for(instance: SweInstance) -> dict[str, Any]:
    """A FakeProvider script that applies the instance's offline solution."""
    task = EvalTask(
        name=instance.instance_id,
        category="swe-bench",
        repo=".",
        prompt=instance.problem_statement,
        check="",
        fake=instance.forge_fake or FakeSolution(),
    )
    return fake_script(task)


async def run_instance(instance: SweInstance, cfg: ForgeConfig, *, fake: bool) -> EvalResult:
    """Clone, solve, apply the test patch, run FAIL_TO_PASS; passed = those tests pass."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "repo"
        try:
            await git(Path(folder), "clone", "-q", clone_url(instance.repo), str(root))
            await git(root, "checkout", "-q", instance.base_commit)
        except RuntimeError as err:
            return EvalResult(
                name=instance.instance_id, passed=False, cost_usd=0.0, seconds=0.0, note=str(err)
            )
        cost = await solve(instance, root, cfg, fake=fake)
        try:
            await git(root, "apply", stdin_text=instance.test_patch)
        except RuntimeError as err:
            return EvalResult(
                name=instance.instance_id,
                passed=False,
                cost_usd=cost,
                seconds=time.monotonic() - started,
                note=str(err),
            )
        tests = await run_argv(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *instance.FAIL_TO_PASS,
            ],
            root,
            timeout_s=TEST_TIMEOUT_S,
        )
    note = "" if tests.code == 0 else (tests.stdout + tests.stderr).strip()[-300:]
    return EvalResult(
        name=instance.instance_id,
        passed=tests.code == 0,
        cost_usd=cost,
        seconds=time.monotonic() - started,
        note=note,
    )


async def solve(instance: SweInstance, root: Path, cfg: ForgeConfig, *, fake: bool) -> float:
    """Let Forge work on the issue headless; returns what it cost."""
    run_cfg = cfg.model_copy(deep=True)
    if fake:
        install_fake(run_cfg, FakeProvider.from_data(fake_for(instance)))
    ctx = await open_session(
        root,
        run_cfg,
        AutoRenderer(),
        store=MemoryStore(),
        executor=LocalExecutor(root.resolve()),
        headless=True,
    )
    try:
        report = await run_task(instance.problem_statement, ctx)
    finally:
        await close_session(ctx)
    return report.usage.cost_usd
