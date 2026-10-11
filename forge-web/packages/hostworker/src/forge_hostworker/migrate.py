"""Migrating a release (W26b): the release is built (and the build kept for its release job),
then each service's migrations run against the app's own database, after the server's backup
job. The release before keeps running meanwhile, so migrations must keep it working (the
template's docs/migrations.md: add first, remove in a later release)."""

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from forge_hostworker.docker import hardened
from forge_hostworker.wire import DeployPlan, JobResult, ServicePlan

if TYPE_CHECKING:  # the runner imports this module
    from forge_hostworker.runner import HostRunner

MIGRATE_TIMEOUT_S = 1800
PATH = "/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def migrate_args(
    plan: DeployPlan, service: ServicePlan, release_dir: Path, network: str, env_file: Path,
    user: str,
) -> list[str]:  # fmt: skip
    """`docker run` for one service's migrations: like the service, but once and then gone."""
    limits = plan.limits
    return [
        "run", "--rm", *hardened(user), "--read-only", "--tmpfs", "/tmp:rw,size=256m",
        "--network", network, "--memory", f"{limits.memory_mb}m", "--cpus", str(limits.cpus),
        "--pids-limit", str(limits.pids), "--env-file", str(env_file),
        "-v", f"{release_dir / service.root}:/app:ro", "-w", "/app",
        "--label", f"forge.app={plan.app}", "--label", "forge.role=migrate",
        service.image, *service.migrate,
    ]  # fmt: skip


async def migrate_release(runner: "HostRunner", plan: DeployPlan, source: Path) -> JobResult:
    """Build the release, then run its migrations on the app's database."""
    release_dir, failed = await runner.prepare(plan, source)
    if failed is not None:
        return failed
    net = await runner.ensure_network(plan)
    url = await runner.database_url(plan)
    logs: list[str] = []
    for service in plan.services:
        if not service.migrate or not url:
            continue
        env_file = runner.data_dir / "tmp" / f"{secrets.token_hex(8)}.env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch(mode=0o600)
        env = {**service.env, "DATABASE_URL": url, "PATH": PATH, "HOME": "/tmp"}
        env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
        try:
            args = migrate_args(plan, service, release_dir, net, env_file, runner.user)
            done = await runner.docker(*args, timeout=MIGRATE_TIMEOUT_S)
        finally:
            env_file.unlink(missing_ok=True)
        logs.append(f"--- {service.name}\n{done.tail()[-8000:]}")
        if done.code != 0:
            return JobResult(ok=False, log_tail="\n".join(logs)[-20_000:],
                             error=f"the migrations of {service.name} failed")  # fmt: skip
    return JobResult(ok=True, log_tail="\n".join(logs)[-20_000:])
