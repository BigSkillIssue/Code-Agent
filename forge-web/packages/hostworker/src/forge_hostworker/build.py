"""Building a release: the packed source unpacked safely, then each service's build command in
a throwaway gVisor container that gets no secrets and reaches only the build network (package
registries; the firewall in `firewall.py` keeps it there).
"""

import hashlib
import os
import tarfile
from pathlib import Path

from forge_hostworker.docker import Docker, DockerResult, hardened
from forge_hostworker.wire import DeployPlan, ServicePlan

BUILD_TIMEOUT_S = 1800
BUILD_MEMORY = "2048m"
BUILD_CPUS = "2"
BUILD_PIDS = "1024"
MAX_SOURCE_BYTES = 2 * 1024**3


class SourceError(Exception):
    """The packed source cannot be unpacked safely."""


def unpack(archive: Path, target: Path) -> None:
    """Unpack a release's .tar.gz; paths and links that leave the folder are refused."""
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > MAX_SOURCE_BYTES:
            raise SourceError("the source is larger than 2 GB")
        for member in members:
            if member.issym() or member.islnk() or member.isdev():
                raise SourceError(f"{member.name}: links and devices are not unpacked")
            parts = member.name.replace("\\", "/").split("/")
            if member.name.startswith(("/", "\\")) or ".." in parts:
                raise SourceError(f"{member.name}: leaves the release's folder")
        try:
            tar.extractall(target, filter="data")
        except tarfile.FilterError as error:
            raise SourceError(str(error)) from error


def file_digest(path: Path) -> str:
    """The SHA-256 of a file (which source a build was made from)."""
    with path.open("rb") as packed:
        return hashlib.file_digest(packed, "sha256").hexdigest()


def hand_over(folder: Path, owner: tuple[int, int]) -> None:
    """Give a release folder to the host user the containers run as, so builds can write it
    (under userns-remap that user is a subordinate id, not the worker's)."""
    if not hasattr(os, "chown"):
        return
    os.chown(folder, *owner)
    for root, folders, files in os.walk(folder):
        for name in [*folders, *files]:
            os.chown(os.path.join(root, name), *owner, follow_symlinks=False)


def build_args(plan: DeployPlan, service: ServicePlan, release_dir: Path, user: str) -> list[str]:
    """`docker run` for one service's build: no secrets, no app network, nothing kept."""
    folder = release_dir / service.root
    labels = ["--label", f"forge.app={plan.app}", "--label", "forge.role=build"]
    return [
        "run",
        "--rm",
        *hardened(user),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,size=1024m",
        "--network",
        plan.build_image_network,
        "--memory",
        BUILD_MEMORY,
        "--cpus",
        BUILD_CPUS,
        "--pids-limit",
        BUILD_PIDS,
        "-v",
        f"{folder}:/app",
        "-w",
        "/app",
        "-e",
        "HOME=/tmp",
        *labels,
        service.image,
        *service.build,
    ]


async def build_release(
    docker: Docker, plan: DeployPlan, release_dir: Path, user: str
) -> tuple[ServicePlan, DockerResult] | None:
    """Build every service; the first one that fails, with what it printed (None: all built)."""
    for service in plan.services:
        if not service.build:
            continue
        result = await docker(
            *build_args(plan, service, release_dir, user), timeout=BUILD_TIMEOUT_S
        )
        if result.code != 0:
            return service, result
    return None
