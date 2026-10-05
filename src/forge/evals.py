"""`forge eval`: run benchmark tasks on sample repos and report pass rate, cost and time."""

import json
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forge.config import ForgeConfig
from forge.local.auto_renderer import AutoRenderer
from forge.local.local_executor import LocalExecutor
from forge.local.memory_store import MemoryStore
from forge.pipeline import run_task
from forge.providers.fake import FakeProvider
from forge.runtime.proc import run_argv
from forge.runtime.shell import find_shell
from forge.wiring import close_session, install_fake, open_session

PYTHON_SLOT = "{python}"


class FakeEdit(BaseModel):
    """One exact replacement the fake coder makes."""

    path: str
    old: str
    new: str


class FakeWrite(BaseModel):
    """One file the fake coder writes."""

    path: str
    content: str


class FakeSolution(BaseModel):
    """What the scripted model does for a task in offline (`--fake`) runs."""

    edits: list[FakeEdit] = []
    writes: list[FakeWrite] = []
    size: str = "trivial"


class EvalTask(BaseModel):
    """A benchmark task: a sample repo, a prompt, and a command that checks the result."""

    name: str
    category: str
    repo: str  # folder relative to the evals folder
    prompt: str
    check: str  # {python} is replaced by the current interpreter
    fake: FakeSolution | None = None


class EvalResult(BaseModel):
    """How one task went."""

    name: str
    passed: bool
    cost_usd: float
    seconds: float
    note: str = ""


def load_tasks(evals_dir: Path, suite: str | None = None) -> list[EvalTask]:
    """Every task in evals/tasks/*.toml whose name or category starts with `suite`."""
    tasks = []
    for path in sorted((evals_dir / "tasks").glob("*.toml")):
        task = EvalTask.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        if suite is None or task.name.startswith(suite) or task.category.startswith(suite):
            tasks.append(task)
    return tasks


def python_command(text: str) -> str:
    """Put the current interpreter where a command says {python}."""
    exe = f'"{Path(sys.executable).as_posix()}"'
    if sys.platform == "win32":
        exe = "& " + exe  # PowerShell runs a quoted path only with the call operator
    return text.replace(PYTHON_SLOT, exe)


def fake_script(task: EvalTask) -> dict[str, Any]:
    """A FakeProvider script that solves the task the way `task.fake` describes."""
    solution = task.fake or FakeSolution()
    spec = {
        "goal": task.prompt,
        "context": "",
        "requirements": [task.prompt],
        "acceptance_criteria": [task.check],
        "size": solution.size,
    }
    reads = [{"name": "read_file", "arguments": {"path": e.path}} for e in solution.edits]
    changes = [{"name": "edit_file", "arguments": e.model_dump()} for e in solution.edits]
    changes += [{"name": "write_file", "arguments": w.model_dump()} for w in solution.writes]
    turns = [{"text": "Looking at the code.", "tool_calls": reads}] if reads else []
    turns += [{"text": "Making the change.", "tool_calls": changes}, {"text": "Done."}]
    return {"roles": {"refiner": [{"text": json.dumps(spec)}]}, "turns": turns}


async def prepare_repo(source: Path, target: Path) -> None:
    """Copy a sample repo and commit it, so diffs and checkpoints work."""
    shutil.copytree(source, target)
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.name=Forge Eval",
            "-c",
            "user.email=eval@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "start",
        ],
    ):
        await run_argv(["git", *args], target)


async def run_eval(
    task: EvalTask, evals_dir: Path, cfg: ForgeConfig, *, fake: bool, mode: str | None = None
) -> EvalResult:
    """Run one task in a fresh copy of its repo (optionally forcing solo/team mode) and check it."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / task.name
        await prepare_repo(evals_dir / task.repo, root)
        run_cfg = cfg.model_copy(deep=True)
        if fake:
            install_fake(run_cfg, FakeProvider.from_data(fake_script(task)))
        ctx = await open_session(
            root,
            run_cfg,
            AutoRenderer(),
            store=MemoryStore(),
            executor=LocalExecutor(root.resolve()),
            headless=True,
        )
        ctx.state.mode_override = mode
        try:
            report = await run_task(task.prompt, ctx)
        finally:
            await close_session(ctx)
        check = await run_argv(_check_argv(python_command(task.check)), root, timeout_s=300)
    note = "" if check.code == 0 else (check.stdout + check.stderr).strip()[-300:]
    return EvalResult(
        name=task.name,
        passed=check.code == 0,
        cost_usd=report.usage.cost_usd,
        seconds=time.monotonic() - started,
        note=note,
    )


def _check_argv(command: str) -> list[str]:
    shell = find_shell("powershell" if sys.platform == "win32" else "bash") or "bash"
    if sys.platform == "win32":
        return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    return [shell, "-c", command]


def compare_table(results: dict[str, list[EvalResult]]) -> str:
    """Tasks as rows, modes as columns, then each mode's pass count, cost and time."""
    modes = list(results)
    names = [r.name for r in results[modes[0]]]
    width = max([len(n) for n in names] + [4])
    lines = ["task".ljust(width) + "".join(f"  {m:<9}" for m in modes)]
    for index, name in enumerate(names):
        marks = ["pass" if results[m][index].passed else "FAIL" for m in modes]
        lines.append(name.ljust(width) + "".join(f"  {mark:<9}" for mark in marks))
    for mode in modes:
        passed = sum(r.passed for r in results[mode])
        cost = sum(r.cost_usd for r in results[mode])
        seconds = sum(r.seconds for r in results[mode])
        lines.append(f"{mode}: passed {passed}/{len(names)}, cost ${cost:.4f}, time {seconds:.1f}s")
    return "\n".join(lines)


def results_table(results: list[EvalResult]) -> str:
    """One line per task, then the pass rate, cost and time."""
    width = max([len(r.name) for r in results] + [4])
    lines = [f"{'task'.ljust(width)}  result  cost      time"]
    for r in results:
        mark = "pass" if r.passed else "FAIL"
        lines.append(f"{r.name.ljust(width)}  {mark}    ${r.cost_usd:<7.4f}  {r.seconds:.1f}s")
    passed = sum(r.passed for r in results)
    cost = sum(r.cost_usd for r in results)
    seconds = sum(r.seconds for r in results)
    lines.append(f"passed {passed}/{len(results)}, cost ${cost:.4f}, time {seconds:.1f}s")
    return "\n".join(lines)
