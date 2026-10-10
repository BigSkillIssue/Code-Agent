"""`forge app dev` and the throwaway PostgreSQL (S65): the product runs as hosting would run
it, every service's output in one stream, and everything stops together."""

import asyncio
import sys
from pathlib import Path

import pytest

from forge.app_dev import run_dev
from forge.app_template import write_app
from forge.ports import Command, CommandResult, SandboxPolicy
from forge.runtime import postgres
from forge.runtime.postgres import PostgresError, start_postgres, stop_postgres
from support import ScriptedExecutor

POLICY = SandboxPolicy(mode="full-access", network=True)


@pytest.fixture
def product(tmp_path: Path) -> Path:
    """A product from the template."""
    root = tmp_path / "tally"
    write_app(root, "tally")
    return root


@pytest.fixture
def fake_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PostgreSQL's programs "installed" in a folder (the executor never runs them)."""
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    monkeypatch.setattr(postgres, "find_bin_dir", lambda: bin_dir)
    monkeypatch.setattr(postgres.os, "geteuid", lambda: 1000, raising=False)
    return bin_dir


async def dev_until(product: Path, executor: ScriptedExecutor, until: str) -> tuple[int, list[str]]:
    """Run `forge app dev` until a line containing `until` is shown; the exit code and lines."""
    shown: list[str] = []
    stop = asyncio.Event()

    def show(line: str) -> None:
        shown.append(line)
        if until in line:
            stop.set()

    code = await asyncio.wait_for(run_dev(product, executor, POLICY, show, stop), 10)
    return code, shown


async def test_the_product_runs_on_a_throwaway_database(product: Path, fake_postgres: Path) -> None:
    lines = {"uv": ["INFO:     Uvicorn running on http://0.0.0.0:8000"], "npm": ["VITE ready"]}
    executor = ScriptedExecutor(job_lines=lines)
    code, shown = await dev_until(product, executor, "VITE ready")
    assert code == 0
    migrate = executor.ran("uv", "run", "--locked", "alembic", "upgrade", "head")[0]
    local = "postgresql://forge@127.0.0.1:" if sys.platform == "win32" else "postgresql://forge@/"
    assert migrate.env["DATABASE_URL"].startswith(local)
    api, web = (c for c in executor.commands if c.cwd and c.timeout_s > 3600)
    assert api.argv[1:5] == ["run", "--locked", "uvicorn", "app.main:create_app"]  # type: ignore[index]
    assert api.env["PORT"] == "8000" and api.env["APP_URL"] == "http://localhost:8080"
    assert api.env["DATABASE_URL"] == migrate.env["DATABASE_URL"]
    assert web.argv[1:] == ["run", "dev", "--", "--port", "8080", "--strictPort"]  # type: ignore[index]
    assert web.env["API_URL"] == "http://localhost:8000"
    assert "[api] INFO:     Uvicorn running on http://0.0.0.0:8000" in shown
    assert "[web] VITE ready" in shown
    assert executor.stopped == ["job1", "job2"]
    assert executor.ran("pg_ctl")[-1].argv[-1] == "stop"  # type: ignore[index]


async def test_a_service_that_stops_stops_everything(product: Path, fake_postgres: Path) -> None:
    executor = ScriptedExecutor(job_lines={"uv": ["Traceback: boom"]})
    executor.ended.add("job1")
    code, shown = await dev_until(product, executor, "never")
    assert code == 1
    assert any("[api] stopped (exit 1)" in line for line in shown)
    assert executor.stopped == ["job1", "job2"]


async def test_a_failed_install_starts_nothing(product: Path, fake_postgres: Path) -> None:
    def answer(cmd: Command) -> CommandResult | None:
        if ScriptedExecutor.words(cmd)[:2] == ["uv", "sync"]:
            return CommandResult(exit_code=2, stdout="", stderr="lockfile out of date")
        return None

    executor = ScriptedExecutor(answer)
    code, shown = await dev_until(product, executor, "never")
    assert code == 1
    assert any("lockfile out of date" in line for line in shown)
    assert not executor.jobs
    assert executor.ran("pg_ctl")[-1].argv[-1] == "stop"  # type: ignore[index]


async def test_a_broken_manifest_starts_nothing(product: Path) -> None:
    (product / "forge.app.toml").write_text("name = 1\n", encoding="utf-8")
    executor = ScriptedExecutor()
    code, shown = await dev_until(product, executor, "never")
    assert code == 1
    assert executor.commands == []
    assert any(line.startswith("forge.app.toml: name") for line in shown)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses localhost, not a socket")
async def test_postgres_starts_with_a_socket_only_and_is_removed(fake_postgres: Path) -> None:
    executor = ScriptedExecutor()
    pg = await start_postgres(executor, POLICY)
    initdb, start, createdb = (c.argv or [] for c in executor.commands)
    assert initdb[0] == str(fake_postgres / postgres._exe("initdb"))
    assert initdb[3:5] == ["-U", "forge"] and "--auth=trust" in initdb
    options = start[start.index("-o") + 1]
    assert f"-k {pg.host}" in options and "listen_addresses=''" in options
    assert start[-2:] == ["-w", "start"]
    assert createdb[-1] == "app"
    assert pg.url == f"postgresql://forge@/app?host={pg.host}&port=5432"
    assert pg.data_dir.parent.is_dir()
    await stop_postgres(executor, POLICY, pg)
    assert (executor.commands[-1].argv or [])[-1] == "stop"
    assert not pg.data_dir.parent.exists()


async def test_postgres_failures_say_what_to_do(
    fake_postgres: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cmd: Command) -> CommandResult | None:
        return CommandResult(exit_code=1, stdout="", stderr="could not bind socket")

    with pytest.raises(PostgresError, match="initdb failed: could not bind socket") as failure:
        await start_postgres(ScriptedExecutor(broken), POLICY)
    assert "--database-url" in failure.value.hint
    monkeypatch.setattr(postgres, "find_bin_dir", lambda: None)
    with pytest.raises(PostgresError, match="not installed"):
        await start_postgres(ScriptedExecutor(), POLICY)


@pytest.mark.skipif(not hasattr(postgres.os, "geteuid"), reason="POSIX only")
async def test_postgres_refuses_root(fake_postgres: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres.os, "geteuid", lambda: 0)
    with pytest.raises(PostgresError, match="root") as failure:
        await start_postgres(ScriptedExecutor(), POLICY)
    assert "normal user" in failure.value.hint


def test_the_newest_installed_postgres_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for version in ("9.6", "16", "14"):
        folder = tmp_path / "postgresql" / version / "bin"
        folder.mkdir(parents=True)
        (folder / postgres._exe("initdb")).write_text("", encoding="utf-8")
    monkeypatch.setattr(postgres.shutil, "which", lambda name: None)
    monkeypatch.setattr(postgres, "SEARCH", (str(tmp_path / "postgresql" / "*" / "bin"),))
    assert postgres.find_bin_dir() == tmp_path / "postgresql" / "16" / "bin"
