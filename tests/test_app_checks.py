"""`forge app check` and the `app_check` tool (S65): fixed gates over a product from the
template, run against a scripted executor; every failed gate names its fix."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from forge.app_checks import REPORT, AppCheckReport, check_app, report_text, save_report
from forge.app_template import write_app
from forge.cli import main
from forge.ports import Command, CommandResult, SandboxPolicy
from forge.providers.base import ToolCall
from forge.runtime import postgres
from forge.runtime.secret_scan import scan_secrets, scan_text
from forge.tools import agent_tools, call_tool
from support import ScriptedExecutor, make_ctx

POLICY = SandboxPolicy(mode="full-access", network=True)
# The throwaway database: a Unix socket, or localhost on Windows.
LOCAL = (
    "postgresql://forge@127.0.0.1:" if sys.platform == "win32" else "postgresql://forge@/app?host="
)
Answer = Callable[[Command], CommandResult | None]


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


def fail_when(*words: str, output: str = "boom") -> Answer:
    """An answer that fails the commands starting with these words."""

    def answer(cmd: Command) -> CommandResult | None:
        if ScriptedExecutor.words(cmd)[: len(words)] == list(words):
            return CommandResult(exit_code=1, stdout=output, stderr="")
        return None

    return answer


async def run_check(
    product: Path, answer: Answer | None = None
) -> tuple[AppCheckReport, ScriptedExecutor]:
    """Check the product with a scripted executor."""
    executor = ScriptedExecutor(answer)
    report = await check_app(product, executor, POLICY)
    return report, executor


def statuses(report: AppCheckReport) -> dict[str, str]:
    """Each gate's status, named `gate` or `gate (service)`."""
    return {f"{g.gate} ({g.service})" if g.service else g.gate: g.status for g in report.gates}


async def test_every_gate_passes(product: Path, fake_postgres: Path) -> None:
    report, executor = await run_check(product)
    assert report.ok, report_text(report)
    assert statuses(report) == {
        "manifest": "passed",
        "secrets": "passed",
        "lockfiles": "passed",
        "database": "passed",
        "migrations (api)": "passed",
        "openapi (api)": "passed",
        "server-tests (api)": "passed",
        "web (web)": "passed",
    }
    tests = executor.ran("uv", "run", "--locked", "pytest")[0]
    assert tests.cwd == str(product / "server")
    assert tests.env["TEST_DATABASE_URL"].startswith(LOCAL)
    upgrade = executor.ran("uv", "run", "--locked", "alembic", "upgrade", "head")[0]
    assert upgrade.env["DATABASE_URL"] == tests.env["TEST_DATABASE_URL"]
    assert executor.ran("uv", "run", "--locked", "alembic", "check")
    for words in (
        ["npm", "ci"],
        ["npm", "run", "api:types"],
        ["npm", "test"],
        ["npm", "run", "build"],
    ):
        assert executor.ran(*words)[0].cwd == str(product / "web")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses localhost, not a socket")
async def test_the_database_is_thrown_away(product: Path, fake_postgres: Path) -> None:
    _, executor = await run_check(product)
    initdb = executor.ran("initdb")[0]
    data = Path(initdb.argv[initdb.argv.index("-D") + 1])  # type: ignore[index, union-attr]
    assert "--auth=trust" in initdb.argv  # type: ignore[operator]
    start = executor.ran("pg_ctl")[0].argv or []
    assert "listen_addresses=''" in start[start.index("-o") + 1]  # socket only, no TCP
    assert executor.ran("pg_ctl")[-1].argv[-1] == "stop"  # type: ignore[index]
    assert not data.parent.exists()


@pytest.mark.parametrize(
    ("words", "gate", "fix"),
    [
        (("uv", "sync"), "server-tests (api)", "uv lock"),
        (("uv", "run", "--locked", "alembic", "upgrade"), "migrations (api)", "failing migration"),
        (("uv", "run", "--locked", "alembic", "check"), "migrations (api)", "--autogenerate"),
        (("uv", "run", "--locked", "python"), "openapi (api)", "app.openapi_snapshot"),
        (("uv", "run", "--locked", "pytest"), "server-tests (api)", "make the tests"),
        (("npm", "ci"), "web (web)", "npm install"),
        (("npm", "test"), "web (web)", "make the tests"),
        (("npm", "run", "build"), "web (web)", "fix the build"),
        (("initdb",), "database", "throwaway database"),
    ],
)
async def test_each_gate_fails_alone_and_names_its_fix(
    product: Path, fake_postgres: Path, words: tuple[str, ...], gate: str, fix: str
) -> None:
    report, _ = await run_check(product, fail_when(*words))
    assert not report.ok
    failed = {name for name, status in statuses(report).items() if status == "failed"}
    assert failed == {gate}
    result = next(
        g for g in report.gates if (f"{g.gate} ({g.service})" if g.service else g.gate) == gate
    )
    assert fix in result.fix
    assert f"fix: {result.fix}" in report_text(report)


async def test_without_postgres_the_server_gates_are_skipped(
    product: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(postgres, "find_bin_dir", lambda: None)
    report, _ = await run_check(product)
    status = statuses(report)
    assert status["database"] == "failed"
    assert "install PostgreSQL" in report.gates[3].fix
    assert status["server-tests (api)"] == "skipped"
    assert status["web (web)"] == "passed"


async def test_a_given_database_is_used_as_it_is(product: Path) -> None:
    executor = ScriptedExecutor()
    report = await check_app(product, executor, POLICY, database_url="postgresql://x@db/test")
    assert report.ok
    assert not executor.ran("initdb")
    assert executor.ran("uv", "run", "--locked", "pytest")[0].env["TEST_DATABASE_URL"] == (
        "postgresql://x@db/test"
    )


async def test_changed_api_types_fail_and_the_file_is_put_back(
    product: Path, fake_postgres: Path
) -> None:
    types = product / "web" / "src" / "api" / "schema.d.ts"
    shipped = types.read_bytes()

    def regenerate(cmd: Command) -> CommandResult | None:
        if ScriptedExecutor.words(cmd)[:3] == ["npm", "run", "api:types"]:
            types.write_text("export interface paths {}\n", encoding="utf-8")
        return None

    report, executor = await run_check(product, regenerate)
    assert statuses(report)["web (web)"] == "failed"
    assert "npm run api:types" in report.gates[-1].fix
    assert types.read_bytes() == shipped
    assert not executor.ran("npm", "test")


async def test_a_committed_secret_fails(product: Path, fake_postgres: Path) -> None:
    key = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
    (product / "server" / "app" / "billing.py").write_text(f'KEY = "{key}"\n', encoding="utf-8")
    (product / ".env").write_text("SECRET=1\n", encoding="utf-8")
    report, _ = await run_check(product)
    gate = next(g for g in report.gates if g.gate == "secrets")
    assert gate.status == "failed"
    assert "server/app/billing.py:1: a Stripe live key" in gate.output
    assert ".env: an environment file" in gate.output
    assert key not in report.model_dump_json()


async def test_missing_lockfiles_fail(product: Path, fake_postgres: Path) -> None:
    (product / "server" / "uv.lock").unlink()
    package = json.loads((product / "web" / "package.json").read_text(encoding="utf-8"))
    package["dependencies"]["left-pad"] = "1.3.0"
    (product / "web" / "package.json").write_text(json.dumps(package), encoding="utf-8")
    report, _ = await run_check(product)
    gate = next(g for g in report.gates if g.gate == "lockfiles")
    assert gate.status == "failed"
    assert "server/uv.lock is missing" in gate.output
    assert "web/package-lock.json differs" in gate.output


async def test_a_broken_manifest_stops_the_check(product: Path) -> None:
    (product / "forge.app.toml").write_text('name = "Bad Name"\n', encoding="utf-8")
    report, executor = await run_check(product)
    assert [g.gate for g in report.gates] == ["manifest"]
    assert "name" in report.gates[0].output
    assert all(ScriptedExecutor.words(c)[0] == "git" for c in executor.commands)


def test_the_report_is_saved(product: Path) -> None:
    report = AppCheckReport(ok=True, commit="abc", gates=[])
    path = save_report(product, report)
    assert path == product / REPORT
    assert json.loads(path.read_text(encoding="utf-8"))["commit"] == "abc"


def test_secret_patterns() -> None:
    lines = {
        "-----BEGIN OPENSSH PRIVATE KEY-----": "a private key",
        "aws = 'AKIAIOSFODNN7EXAMPLE'": "an AWS access key",
        "token: ghp_" + "a" * 36: "a GitHub token",
        "postgresql://app:hunter2secret@db.example.com/app": "a database URL with a password",
    }
    for line, kind in lines.items():
        assert [f.kind for f in scan_text("x", line)] == [kind], line
    harmless = [
        "postgresql://postgres:dev@localhost:5432/app",
        "postgresql://postgres:dev@db:5432/app",
        "risk-assessment and task-management",
        '"integrity": "sha512-abc"',
    ]
    for line in harmless:
        assert scan_text("x", line) == [], line


async def test_example_env_files_are_fine(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (tmp_path / "deploy.pem").write_text("x\n", encoding="utf-8")
    assert [(f.path, f.kind) for f in await scan_secrets(tmp_path)] == [
        ("deploy.pem", "a key or certificate file")
    ]


async def test_the_tool_is_offered_only_in_products(product: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "app_check" not in [t.name for t in agent_tools(make_ctx(plain), "coder")]
    assert "app_check" in [t.name for t in agent_tools(make_ctx(product), "coder")]
    assert "app_check" not in [t.name for t in agent_tools(make_ctx(product), "reviewer")]


async def test_the_tool_reports_and_saves(product: Path, fake_postgres: Path) -> None:
    ctx = make_ctx(product, executor=ScriptedExecutor(fail_when("npm", "test")))
    result = await call_tool(ctx, ToolCall(id="c1", name="app_check", arguments={}))
    assert not result.ok
    assert result.code == "exit_nonzero"
    assert result.text.startswith("app check: failed (1 gate(s))")
    assert "FAILED  web (web)" in result.text
    saved = json.loads((product / REPORT).read_text(encoding="utf-8"))
    assert saved["ok"] is False
    assert ctx.renderer.approval_requests  # type: ignore[attr-defined]


def test_forge_app_check_prints_and_exits(
    product: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_check(
        root: Path, executor: object, policy: object, url: str | None, on_gate: object
    ) -> AppCheckReport:
        assert url == "postgresql://x@h/db"
        return AppCheckReport(ok=False, commit="abc", gates=[])

    monkeypatch.setattr("forge.app_cli.check_app", fake_check)
    code = main(["-C", str(product), "app", "check", "--database-url", "postgresql://x@h/db"])
    assert code == 1
    assert "app check: failed" in capsys.readouterr().out
    assert (product / REPORT).is_file()


async def test_programs_found_with_windows_names_still_count(
    product: Path, fake_postgres: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows shutil.which answers `C:\\...\\npm.CMD`; the gates must work the same."""
    monkeypatch.setattr(
        "forge.app_checks.shutil.which",
        lambda name: f"C:/Tools/{name}.{'CMD' if name == 'npm' else 'EXE'}",
    )
    report, executor = await run_check(product, fail_when("npm", "test"))
    assert statuses(report)["web (web)"] == "failed"
    assert executor.ran("npm", "ci")[0].argv[0] == "C:/Tools/npm.CMD"  # type: ignore[index]
