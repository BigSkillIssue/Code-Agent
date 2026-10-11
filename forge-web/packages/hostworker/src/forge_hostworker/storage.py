"""An app's own files (`[storage]` in its manifest): one Docker volume per app and environment,
mounted at `/data` in its services (not in static ones) and given to the user they run as."""

from forge_hostworker.docker import Docker, HostError
from forge_hostworker.wire import DeployPlan


def files_volume(app: str, environment: str) -> str:
    """The volume of an app's files."""
    return f"forge-{app}-{environment}-files"


def files_mount(plan: DeployPlan, runtime: str) -> list[str]:
    """The `docker run` arguments that mount the app's files (none without storage)."""
    if not plan.storage_gb or runtime == "static":
        return []
    return ["-v", f"{files_volume(plan.app, plan.environment)}:/data"]


async def ensure_files(docker: Docker, plan: DeployPlan, user: str, image: str) -> None:
    """Make the volume once and hand it to the containers' user (a volume starts as root's)."""
    if not plan.storage_gb:
        return
    name = files_volume(plan.app, plan.environment)
    if (await docker("volume", "inspect", name)).code == 0:
        return
    made = await docker("volume", "create", "--label", f"forge.app={plan.app}", name)
    given = await docker(
        "run", "--rm", "--runtime=runsc", "--cap-drop", "ALL", "--cap-add", "CHOWN",
        "--security-opt", "no-new-privileges", "--network", "none", "--user", "0:0",
        "-v", f"{name}:/data", image, "chown", user, "/data",
    )  # fmt: skip
    if made.code != 0 or given.code != 0:
        await docker("volume", "rm", name)
        raise HostError(
            f"the app's files could not be set up: {(made if made.code else given).tail()[-300:]}",
            hint="see the Docker daemon's log",
        )
