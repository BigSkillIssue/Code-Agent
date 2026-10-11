"""`forge-host-worker doctor`: is this host ready to run apps? Every missing piece is named
with what to do about it."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from forge_hostworker.docker import Docker, HostError, hardened, require_runsc, uses_userns
from forge_hostworker.edge import CADDY_ADMIN
from forge_hostworker.firewall import BUILD_SUBNET, CHAIN, HOST_CHAIN, Run, _run

MIN_FREE_GB = 10
GUIDE = "docs/EINRICHTUNG.md §11"
TEST_IMAGE = "mirror.gcr.io/library/busybox:1.37"


@dataclass(frozen=True)
class Check:
    """One check and its outcome."""

    name: str
    ok: bool
    detail: str
    fix: str = ""


async def docker_checks(
    docker: Docker, image: str = TEST_IMAGE, root: bool | None = None
) -> list[Check]:
    """Docker, gVisor, user namespaces, a hardened test container and the build network."""
    try:
        await require_runsc(docker)
        checks = [Check("gVisor", True, "Docker has the runsc runtime")]
    except HostError as error:
        return [Check("gVisor", False, str(error), error.hint)]
    userns = await uses_userns(docker)
    checks.append(
        Check(
            "user namespaces",
            userns,
            "containers run in their own user namespace"
            if userns
            else "Docker runs without userns-remap",
            "" if userns else f'set "userns-remap": "default" in daemon.json ({GUIDE})',
        )
    )
    checks.append(worker_check(userns, root))
    checks.append(await sandbox_check(docker, image))
    subnet = await docker(
        "network", "inspect", "forge-build", "-f", "{{(index .IPAM.Config 0).Subnet}}"
    )
    built = subnet.code == 0 and subnet.out.strip() == BUILD_SUBNET
    checks.append(
        Check(
            "build network",
            built,
            f"forge-build is {BUILD_SUBNET}"
            if built
            else "the network forge-build is missing or has another subnet",
            "" if built else f"docker network create --subnet {BUILD_SUBNET} forge-build",
        )
    )
    return checks


def is_root() -> bool:
    """Whether this process runs as root."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def worker_check(userns: bool, root: bool | None = None) -> Check:
    """Under userns-remap only root can hand the release folders to the containers' user."""
    ok = not userns or (is_root() if root is None else root)
    return Check(
        "worker user",
        ok,
        "the worker can hand release folders to the containers' user"
        if ok
        else "Docker remaps users, but the worker does not run as root",
        "" if ok else "run the worker as root (deploy/host/forge-host-worker.service)",
    )


async def sandbox_check(docker: Docker, image: str) -> Check:
    """Start one container with every flag an app gets: proves gVisor works with the rest."""
    started = await docker(
        "run", "--rm", *hardened("1000:1000"), "--read-only", "--network", "none",
        "--pids-limit", "64", "--memory", "64m", image, "true", timeout=300,
    )  # fmt: skip
    ok = started.code == 0
    return Check(
        "sandbox",
        ok,
        "a hardened gVisor container starts"
        if ok
        else f"a hardened gVisor container does not start: {started.tail()[-300:]}",
        "" if ok else f"see Docker's log (journalctl -u docker) and {GUIDE}",
    )


def firewall_check(run: Run = _run, root: bool | None = None) -> Check:
    """Whether DOCKER-USER and INPUT jump to Forge's chains (only root can tell)."""
    if not (is_root() if root is None else root):
        return Check("firewall", True, "not checked: run doctor as root to check the firewall")
    missing = [
        f"{start} -> {chain}"
        for start, chain in (("DOCKER-USER", CHAIN), ("INPUT", HOST_CHAIN))
        if run(["iptables", "-C", start, "-j", chain], "") != 0
    ]
    return Check(
        "firewall",
        not missing,
        f"DOCKER-USER jumps to {CHAIN}, INPUT to {HOST_CHAIN}"
        if not missing
        else f"missing: {', '.join(missing)}",
        "" if not missing else "sudo forge-host-worker firewall --apply",
    )


async def caddy_check(admin: str, transport: httpx.AsyncBaseTransport | None = None) -> Check:
    """Whether Caddy's admin API answers on localhost."""
    try:
        async with httpx.AsyncClient(base_url=admin, timeout=5, transport=transport) as client:
            ok = (await client.get("/config/")).status_code == 200
    except httpx.HTTPError:
        ok = False
    return Check(
        "Caddy",
        ok,
        "Caddy's admin API answers" if ok else f"no Caddy at {admin}",
        "" if ok else f"install Caddy and start it with deploy/host/Caddyfile ({GUIDE})",
    )


def data_checks(data: Path, min_free_gb: float = MIN_FREE_GB) -> list[Check]:
    """The host key, the token and free disk space."""
    key = (data / "host.key").is_file()
    token = (data / "token").is_file() or bool(os.environ.get("FORGE_HOST_TOKEN"))
    free = shutil.disk_usage(data if data.exists() else data.parent).free / 1024**3
    return [
        Check(
            "host key",
            key,
            "there is a host key" if key else "no host key",
            "" if key else f"forge-host-worker keygen --data {data}",
        ),
        Check(
            "token",
            token,
            "there is a host token" if token else "no host token",
            ""
            if token
            else f"make one in Forge Web (Admin, Hosting) and save it as {data / 'token'}",
        ),
        Check(
            "disk",
            free >= min_free_gb,
            f"{free:.0f} GB free",
            "" if free >= min_free_gb else f"free at least {min_free_gb:g} GB",
        ),
    ]


async def doctor(
    docker: Docker,
    data: Path,
    *,
    admin: str = CADDY_ADMIN,
    transport: httpx.AsyncBaseTransport | None = None,
    run: Run = _run,
    root: bool | None = None,
    image: str = TEST_IMAGE,
    min_free_gb: float = MIN_FREE_GB,
) -> list[Check]:
    """Every check of the host."""
    return [
        *await docker_checks(docker, image, root),
        firewall_check(run, root),
        await caddy_check(admin, transport),
        *data_checks(data, min_free_gb),
    ]


def report(checks: list[Check]) -> str:
    """The checks for people: one line each, the fix below a failed one."""
    lines = []
    for check in checks:
        lines.append(f"{'ok  ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"     fix: {check.fix}")
    return "\n".join(lines)
