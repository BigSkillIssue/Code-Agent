"""The fixed gates a product passes before it is reviewed and hosted (S65): `forge app check`
and the `app_check` tool.

Gates: the manifest, secrets, lockfiles, then per Python service its migrations (upgrade to head
on an empty database, nothing left for autogenerate), its API snapshot and its tests on
PostgreSQL, and per web client its install, generated API types, tests and build. Every failed
gate names its fix. The result is saved in `.forge/out/app/checks.json`; hosting binds it to the
commit it was made on.
"""

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from forge.app_manifest import MANIFEST, AppManifest, Service, load_manifest
from forge.ports import Command, CommandResult, Executor, SandboxPolicy
from forge.runtime.postgres import Postgres, PostgresError, start_postgres, stop_postgres
from forge.runtime.secret_scan import scan_secrets

REPORT = Path(".forge") / "out" / "app" / "checks.json"
INSTALL_TIMEOUT_S = 900
RUN_TIMEOUT_S = 900
OUTPUT_TAIL = 3_000
SNAPSHOT_CHECK = (
    "import sys\nfrom app.openapi_snapshot import SNAPSHOT, current, render\n"
    "sys.exit(0 if SNAPSHOT.read_text(encoding='utf-8') == render(current()) else 1)"
)
Status = Literal["passed", "failed", "skipped"]


class GateResult(BaseModel):
    """One gate's outcome; a failed gate always says how to fix it."""

    gate: str
    service: str = ""
    status: Status
    summary: str
    fix: str = ""
    output: str = ""  # the end of what the commands printed
    seconds: float = 0.0


class AppCheckReport(BaseModel):
    """Every gate of one check of the product."""

    ok: bool
    commit: str = ""  # HEAD when the check ran
    dirty: bool = False  # uncommitted changes were checked too
    checked_at: float = 0.0
    gates: list[GateResult] = []


@dataclass
class Checker:
    """Runs the gates of one product through the Executor port."""

    root: Path
    executor: Executor
    policy: SandboxPolicy
    on_gate: Callable[[GateResult], None] | None = None
    gates: list[GateResult] = field(default_factory=list)

    def record(
        self,
        gate: str,
        status: Status,
        summary: str,
        *,
        service: Service | None = None,
        fix: str = "",
        output: str = "",
        started: float | None = None,
    ) -> None:
        """Record a gate's outcome and report it to whoever watches."""
        seconds = time.monotonic() - started if started is not None else 0.0
        result = GateResult(
            gate=gate,
            service=service.name if service else "",
            status=status,
            summary=summary,
            fix=fix,
            output=output,
            seconds=round(seconds, 1),
        )
        self.gates.append(result)
        if self.on_gate is not None:
            self.on_gate(result)

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout_s: float = RUN_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run one command in the product."""
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]  # npm is npm.cmd on Windows
        cmd = Command(argv=argv, cwd=str(cwd), timeout_s=timeout_s, env=env or {})
        result = await self.executor.run(cmd, self.policy)
        if result.timed_out and result.job_id:
            await self.executor.job_stop(result.job_id)
        return result


def tail(result: CommandResult) -> str:
    """The end of a command's output."""
    text = "\n".join(t for t in (result.stdout.rstrip(), result.stderr.rstrip()) if t)
    return text[-OUTPUT_TAIL:]


def failed(result: CommandResult) -> bool:
    """True unless the command ran to the end and exited 0."""
    return result.timed_out or result.exit_code != 0


async def check_app(
    root: Path,
    executor: Executor,
    policy: SandboxPolicy,
    database_url: str | None = None,
    on_gate: Callable[[GateResult], None] | None = None,
) -> AppCheckReport:
    """Run every gate of the product in `root`."""
    checker = Checker(root, executor, policy, on_gate)
    manifest = load_manifest(root)
    if isinstance(manifest, list):
        problems = "\n".join(f"{p.field or MANIFEST}: {p.message}" for p in manifest)
        summary = f"{MANIFEST} has {len(manifest)} problem(s)"
        checker.record("manifest", "failed", summary, fix=f"fix {MANIFEST}", output=problems)
    else:
        checker.record("manifest", "passed", f"{MANIFEST} is valid")
        await secrets_gate(checker)
        lockfiles_gate(checker, manifest)
        await python_gates(checker, manifest, database_url)
        for service in web_services(root, manifest):
            await web_gate(checker, service)
    commit, dirty = await head_commit(checker)
    ok = all(gate.status != "failed" for gate in checker.gates)
    return AppCheckReport(
        ok=ok, commit=commit, dirty=dirty, checked_at=time.time(), gates=checker.gates
    )


async def secrets_gate(checker: Checker) -> None:
    """No key, token or private key in the product's files."""
    findings = await scan_secrets(checker.root)
    if not findings:
        checker.record("secrets", "passed", "no secrets in the files")
        return
    where = "\n".join(f"{f.path}{f':{f.line}' if f.line else ''}: {f.kind}" for f in findings)
    fix = (
        "remove them (and from git history if committed), rotate them, list their names under "
        f"`secrets` in {MANIFEST} and set the values in Forge Web"
    )
    checker.record(
        "secrets", "failed", f"{len(findings)} secret(s) in the files", fix=fix, output=where
    )


def lockfiles_gate(checker: Checker, manifest: AppManifest) -> None:
    """Every service pins its packages in a lockfile."""
    missing: list[str] = []
    for service in manifest.services:
        folder = checker.root / service.root
        if (folder / "pyproject.toml").is_file() and not (folder / "uv.lock").is_file():
            missing.append(f"{service.root}/uv.lock is missing (run `uv lock` in {service.root})")
        if (folder / "package.json").is_file():
            missing += _npm_lock_problems(folder, service.root)
    if missing:
        summary = "lockfiles are missing or out of date"
        fix = "make and commit the lockfiles named below"
        checker.record("lockfiles", "failed", summary, fix=fix, output="\n".join(missing))
    else:
        checker.record("lockfiles", "passed", "every service is locked")


def _npm_lock_problems(folder: Path, root: str) -> list[str]:
    """What is wrong with a web client's package-lock.json."""
    lock_file = folder / "package-lock.json"
    if not lock_file.is_file():
        return [f"{root}/package-lock.json is missing (run `npm install` in {root})"]
    package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    lock = json.loads(lock_file.read_text(encoding="utf-8")).get("packages", {}).get("", {})
    for key in ("dependencies", "devDependencies"):
        if package.get(key, {}) != lock.get(key, {}):
            return [f"{root}/package-lock.json differs from package.json (run `npm install`)"]
    return []


async def python_gates(checker: Checker, manifest: AppManifest, url: str | None) -> None:
    """Migrations, API snapshot and tests of every Python service, on one throwaway database."""
    services = [s for s in manifest.services if s.runtime == "python3.12"]
    if not services:
        return
    pg: Postgres | None = None
    if manifest.database is None:
        url = ""
    elif url is None:
        try:
            pg = await start_postgres(checker.executor, checker.policy)
            url = pg.url
            checker.record("database", "passed", "a throwaway PostgreSQL is running")
        except PostgresError as error:
            checker.record("database", "failed", str(error), fix=error.hint)
    try:
        for service in services:
            await python_service(checker, service, url)
    finally:
        if pg is not None:
            await stop_postgres(checker.executor, checker.policy, pg)


async def python_service(checker: Checker, service: Service, url: str | None) -> None:
    """Install, then migrations, API snapshot and tests of one Python service."""
    folder = checker.root / service.root
    started = time.monotonic()
    installed = await checker.run(["uv", "sync", "--locked"], folder, INSTALL_TIMEOUT_S)
    if failed(installed):
        fix = f"run `uv lock` in {service.root} and commit uv.lock, or fix pyproject.toml"
        checker.record(
            "server-tests",
            "failed",
            "the packages could not be installed",
            service=service,
            fix=fix,
            output=tail(installed),
            started=started,
        )
        return
    if url is None:
        for gate in ("migrations", "openapi", "server-tests"):
            checker.record(gate, "skipped", "no database (see the database gate)", service=service)
        return
    await migrations_gate(checker, service, folder, url)
    await openapi_gate(checker, service, folder)
    await tests_gate(checker, service, folder, url)


async def migrations_gate(checker: Checker, service: Service, folder: Path, url: str) -> None:
    """Every migration applies to an empty database and nothing is left for autogenerate."""
    if not (folder / "alembic.ini").is_file():
        checker.record("migrations", "skipped", "no alembic.ini", service=service)
        return
    started = time.monotonic()
    env = {"DATABASE_URL": url} if url else {}
    alembic = ["uv", "run", "--locked", "alembic"]
    upgrade = await checker.run([*alembic, "upgrade", "head"], folder, env=env)
    if failed(upgrade):
        fix = f"fix the failing migration in {service.root}/migrations/versions (error below)"
        checker.record(
            "migrations",
            "failed",
            "a migration does not apply",
            service=service,
            fix=fix,
            output=tail(upgrade),
            started=started,
        )
        return
    check = await checker.run([*alembic, "check"], folder, env=env)
    if failed(check):
        fix = (
            f'add a migration: `uv run alembic revision --autogenerate -m "..."` in '
            f"{service.root}, read it, and follow docs/migrations.md"
        )
        checker.record(
            "migrations",
            "failed",
            "the models changed without a migration",
            service=service,
            fix=fix,
            output=tail(check),
            started=started,
        )
        return
    checker.record(
        "migrations",
        "passed",
        "migrations apply and match the models",
        service=service,
        started=started,
    )


async def openapi_gate(checker: Checker, service: Service, folder: Path) -> None:
    """The committed openapi.json describes the server's API as it is."""
    if not (folder / "app" / "openapi_snapshot.py").is_file():
        checker.record("openapi", "skipped", "no app/openapi_snapshot.py", service=service)
        return
    started = time.monotonic()
    result = await checker.run(["uv", "run", "--locked", "python", "-c", SNAPSHOT_CHECK], folder)
    if failed(result):
        fix = (
            f"run `uv run python -m app.openapi_snapshot` in {service.root}, then "
            "`npm run api:types` in the web client, and commit both"
        )
        checker.record(
            "openapi",
            "failed",
            "openapi.json differs from the API",
            service=service,
            fix=fix,
            output=tail(result),
            started=started,
        )
        return
    checker.record(
        "openapi", "passed", "openapi.json matches the API", service=service, started=started
    )


async def tests_gate(checker: Checker, service: Service, folder: Path, url: str) -> None:
    """The service's own tests pass on PostgreSQL."""
    if not (folder / "tests").is_dir():
        checker.record(
            "server-tests",
            "failed",
            "the service has no tests",
            service=service,
            fix=f"add tests in {service.root}/tests",
        )
        return
    started = time.monotonic()
    env = {"TEST_DATABASE_URL": url} if url else {}
    result = await checker.run(["uv", "run", "--locked", "pytest", "-q"], folder, env=env)
    last = (result.stdout.strip().splitlines() or [""])[-1]  # pytest's summary line
    if failed(result):
        fix = f"make the tests in {service.root}/tests pass (output below)"
        checker.record(
            "server-tests",
            "failed",
            last or "the tests failed",
            service=service,
            fix=fix,
            output=tail(result),
            started=started,
        )
        return
    checker.record(
        "server-tests", "passed", last or "the tests passed", service=service, started=started
    )


def web_services(root: Path, manifest: AppManifest) -> list[Service]:
    """Services built with npm (a package.json in their folder)."""
    return [s for s in manifest.services if (root / s.root / "package.json").is_file()]


async def web_gate(checker: Checker, service: Service) -> None:
    """Install, generated API types, tests and build of one web client."""
    folder = checker.root / service.root
    scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    started = time.monotonic()
    steps = [
        (
            ["npm", "ci", "--no-audit", "--no-fund"],
            f"run `npm install` in {service.root} and commit package-lock.json",
        )
    ]
    if "test" in scripts:
        steps.append((["npm", "test"], f"make the tests in {service.root} pass (output below)"))
    if "build" in scripts:
        steps.append((["npm", "run", "build"], f"fix the build of {service.root} (errors below)"))
    for index, (argv, fix) in enumerate(steps):
        result = await checker.run(argv, folder, INSTALL_TIMEOUT_S)
        if failed(result):
            checker.record(
                "web",
                "failed",
                f"`{' '.join(argv)}` failed",
                service=service,
                fix=fix,
                output=tail(result),
                started=started,
            )
            return
        if index == 0 and "api:types" in scripts and not await types_match(checker, folder):
            fix = f"run `npm run api:types` in {service.root} and commit the generated types"
            checker.record(
                "web",
                "failed",
                "the API types differ from openapi.json",
                service=service,
                fix=fix,
                started=started,
            )
            return
    checker.record("web", "passed", "installed, tested and built", service=service, started=started)


async def types_match(checker: Checker, folder: Path) -> bool:
    """Whether regenerating the API types changes nothing (the file is put back either way)."""
    package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    words = package["scripts"]["api:types"].split()
    if "--output" not in words or words.index("--output") + 1 >= len(words):
        return True  # cannot tell where the types go
    target = folder / words[words.index("--output") + 1]
    before = target.read_bytes() if target.is_file() else b""
    result = await checker.run(["npm", "run", "api:types"], folder)
    after = target.read_bytes() if target.is_file() else b""
    target.write_bytes(before)
    return not failed(result) and before == after


async def head_commit(checker: Checker) -> tuple[str, bool]:
    """The commit the check ran on, and whether there were uncommitted changes."""
    head = await checker.run(["git", "rev-parse", "HEAD"], checker.root, 30)
    if failed(head):
        return "", True
    status = await checker.run(["git", "status", "--porcelain"], checker.root, 30)
    return head.stdout.strip(), bool(status.stdout.strip())


def save_report(root: Path, report: AppCheckReport) -> Path:
    """Write the report to `.forge/out/app/checks.json`."""
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def report_text(report: AppCheckReport) -> str:
    """The report for people and for the agent: one line per gate, fixes and output below."""
    failures = [g for g in report.gates if g.status == "failed"]
    head = "app check: passed" if report.ok else f"app check: failed ({len(failures)} gate(s))"
    lines = [head]
    for gate in report.gates:
        name = f"{gate.gate} ({gate.service})" if gate.service else gate.gate
        lines.append(f"{gate.status.upper():7} {name}: {gate.summary}")
        if gate.status == "failed":
            lines.append(f"        fix: {gate.fix}")
            lines += [f"        | {line}" for line in gate.output.splitlines()[-40:]]
    return "\n".join(lines)
