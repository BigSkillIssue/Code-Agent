"""What an admin does with a running app (W27): start a suspended app's live release again, and
read the end of its services' logs. Stopping it is the runner's `stop`."""

from typing import TYPE_CHECKING

from forge_hostworker.wire import JobResult

if TYPE_CHECKING:  # the runner imports this module
    from forge_hostworker.runner import HostRunner

LOG_LINES = "200"


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
