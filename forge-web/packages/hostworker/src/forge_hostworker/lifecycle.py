"""What an admin does with a running app (W27): start a suspended app's live release again, and
read the end of its services' logs. Stopping it is the runner's `stop`. The customer's own
domains of an app (W30), which the edge serves too. And the end of an app on this host (W29c):
its deletion."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from forge_hostworker import postgres
from forge_hostworker.storage import files_volume
from forge_hostworker.wire import JobResult

if TYPE_CHECKING:  # the runner imports this module
    from forge_hostworker.runner import HostRunner

LOG_LINES = "200"
DOMAINS_FILE = "domains.json"  # in the app's folder


async def start_live(runner: "HostRunner", app: str, environment: str) -> JobResult:
    """Start the live release's containers again (after a suspension)."""
    live = runner.state(app, environment).live
    names = await runner.containers(app, environment, live) if live else []
    if not names:
        return JobResult(ok=False, error="the app has no live release on this host")
    started = await runner.docker("start", *names)
    if started.code != 0:
        return JobResult(ok=False, release=live or 0, error="the app could not start",
                         log_tail=started.tail())  # fmt: skip
    return JobResult(ok=True, release=live or 0)


async def live_logs(runner: "HostRunner", app: str, environment: str) -> JobResult:
    """The end of the live release's logs, service by service."""
    live = runner.state(app, environment).live
    names = await runner.containers(app, environment, live) if live else []
    parts = []
    for name in names:
        found = await runner.docker("logs", "--tail", LOG_LINES, name)
        parts.append(f"--- {name}\n{found.tail()[-6000:]}")
    return JobResult(ok=bool(names), release=live or 0, log_tail="\n".join(parts)[-20_000:],
                     error="" if names else "the app has no live release on this host")  # fmt: skip


async def delete_app(runner: "HostRunner", app: str, environment: str) -> JobResult:
    """Remove everything of the app here: its containers, network, database and volumes, its
    folder (releases, state, the database password). The edge then drops it."""
    from forge_hostworker.runner import network  # the runner imports this module

    database = postgres.container(app, environment)
    labelled = await runner.docker("ps", "-a", "-q", "--filter", f"label=forge.app={app}",
                                   "--filter", f"label=forge.env={environment}")  # fmt: skip
    await runner.docker("rm", "-f", *labelled.out.split(), database)
    await runner.docker("network", "rm", network(app, environment))
    await runner.docker("volume", "rm", "-f", files_volume(app, environment), f"{database}-data")
    await asyncio.to_thread(shutil.rmtree, runner.folder(app, environment), True)
    left = await runner.docker("ps", "-a", "-q", "--filter", f"label=forge.app={app}",
                               "--filter", f"label=forge.env={environment}")  # fmt: skip
    gone = await runner.docker("volume", "inspect", files_volume(app, environment))
    if left.out.strip() or gone.code == 0:
        return JobResult(ok=False, error="parts of the app could not be removed",
                         log_tail=left.out[-2000:])  # fmt: skip
    return JobResult(ok=True)


async def set_domains(
    runner: "HostRunner", app: str, environment: str, domains: list[str]
) -> JobResult:
    """Keep the app's own domains (each verified by Forge Web); they replace the ones before."""
    folder = runner.folder(app, environment)
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(sorted(set(domains)))
    await asyncio.to_thread((folder / DOMAINS_FILE).write_text, text, "utf-8")
    return JobResult(ok=True)


def saved_domains(folder: Path) -> list[str]:
    """The app's own domains, as `set_domains` kept them."""
    try:
        found = json.loads((folder / DOMAINS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [domain for domain in found if isinstance(domain, str)]
