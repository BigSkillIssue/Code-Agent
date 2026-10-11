"""What a deploy runs: the approved commit, packed by the project's sandbox; its `forge.app.toml`;
and the host's plan resolved from it.

The plan holds only what the server decided: the admin's fixed runtime images, the product's
commands, ports and routes, limits by resource class and the *names* of secrets. The host's own
checks follow the same conventions as `forge app check`: a Python service with `alembic.ini` is
migrated with `alembic upgrade head`, one with `tests/` is tested with pytest (installed with its
dev dependencies), a Node or static service with a `test` script with `npm test`.
"""

import base64
import contextlib
import json
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.app_manifest import MANIFEST, AppManifest, Service, parse_manifest
from pydantic import ValidationError

from forge_hostworker.wire import DeployPlan, Limits, ServicePlan, app_host
from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.containers.driver import SandboxError

SandboxCall = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
SANDBOX_ERRORS = (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError)
PART = 512 * 1024  # bytes per sandbox message while the commit's files come over
MAX_TEXT = 1024 * 1024  # forge.app.toml and package.json are read only up to this size
LIMITS = {
    "small": Limits(cpus=0.5, memory_mb=512, pids=256),
    "medium": Limits(cpus=1, memory_mb=1024, pids=512),
    "large": Limits(cpus=2, memory_mb=2048, pids=1024),
}
MIGRATE = ["alembic", "upgrade", "head"]


class PlanProblem(Exception):
    """The commit cannot be deployed as it is; with a hint for the user."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Product:
    """What the server reads from a packed commit."""

    manifest: AppManifest
    files: frozenset[str]
    scripts: dict[str, frozenset[str]]  # folder ("" for the root) -> its package.json scripts


async def pack_commit(
    call: SandboxCall, project_id: str, commit: str, target: Path, max_bytes: int
) -> Path:
    """The commit's files, packed by the project's sandbox and read over in parts."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        packed = await call(project_id, "git.archive", {"commit": commit})
        path = str(packed["path"])
        if int(packed.get("size", 0)) > max_bytes:
            raise PlanProblem("the product is larger than a deploy may be",
                              f"at most {max_bytes // (1024 * 1024)} MB")  # fmt: skip
        with target.open("wb") as out:
            offset = 0
            while True:
                part = await call(project_id, "fs.read",
                                  {"path": path, "offset": offset, "limit": PART})  # fmt: skip
                chunk = part_bytes(part)
                out.write(chunk)
                offset += len(chunk)
                if not part.get("truncated") or not chunk or offset > max_bytes:
                    break
        with contextlib.suppress(*SANDBOX_ERRORS):
            await call(project_id, "fs.delete", {"path": path})
    except SANDBOX_ERRORS as err:
        raise PlanProblem(f"the project's sandbox could not pack the commit: {err}",
                          "open the project once so its sandbox starts") from None  # fmt: skip
    return target


def part_bytes(part: dict[str, Any]) -> bytes:
    """The bytes of one read result (text or base64)."""
    if isinstance(part.get("base64"), str):
        return base64.b64decode(part["base64"])
    return str(part.get("text") or "").encode("utf-8")


def read_product(archive: Path) -> Product:
    """The manifest, the file names and the package scripts of a packed commit."""
    texts: dict[str, str] = {}
    files: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.removeprefix("./")
                if not member.isfile():
                    continue
                files.add(name)
                wanted = name == MANIFEST or name.rsplit("/", 1)[-1] == "package.json"
                if wanted and "node_modules/" not in name and member.size <= MAX_TEXT:
                    data = tar.extractfile(member)
                    texts[name] = data.read().decode("utf-8", "replace") if data else ""
    except (tarfile.TarError, OSError, EOFError) as err:
        raise PlanProblem(f"the packed commit cannot be read: {err}") from None
    if MANIFEST not in texts:
        raise PlanProblem(f"the commit has no {MANIFEST}", "an app project has one at its root")
    parsed = parse_manifest(texts[MANIFEST])
    if isinstance(parsed, list):
        found = "; ".join(f"{p.field or MANIFEST}: {p.message}" for p in parsed[:5])
        raise PlanProblem(f"{MANIFEST} is not valid: {found}",
                          "ask Forge to fix it; `forge app check` shows the same")  # fmt: skip
    scripts = {
        name.removesuffix("package.json").rstrip("/"): package_scripts(text)
        for name, text in texts.items()
        if name.endswith("package.json")
    }
    return Product(parsed, frozenset(files), scripts)


def package_scripts(text: str) -> frozenset[str]:
    """The script names of a package.json (none when it cannot be read)."""
    with contextlib.suppress(ValueError):
        scripts = json.loads(text).get("scripts")
        if isinstance(scripts, dict):
            return frozenset(str(name) for name in scripts)
    return frozenset()


def folder(root: str) -> str:
    """A service's root as a prefix of file names ("" for the product's root)."""
    cleaned = root.replace("\\", "/").strip("/")
    return "" if cleaned in ("", ".") else cleaned + "/"


def service_plan(
    service: Service, product: Product, images: dict[str, str], env: dict[str, str]
) -> ServicePlan:
    """One service of the plan, with the host's checks and the migrations it needs."""
    image = images.get(service.runtime)
    if not image:
        raise PlanProblem(f"this server has no runtime image for {service.runtime}",
                          "an admin sets it under [hosting.images]")  # fmt: skip
    prefix, manifest = folder(service.root), product.manifest
    python = service.runtime == "python3.12"
    has_tests = python and any(name.startswith(prefix + "tests/") for name in product.files)
    npm_test = not python and "test" in product.scripts.get(prefix.rstrip("/"), frozenset())
    dev_build = [part for part in service.build if part != "--no-dev"]
    migrates = python and manifest.database is not None and prefix + "alembic.ini" in product.files
    return ServicePlan(
        name=service.name, runtime=service.runtime, image=image, root=service.root,
        build=service.build, command=service.command, output=service.output, port=service.port,
        health=service.health, route=service.route, env=env,
        secrets=[] if service.runtime == "static" else manifest.secrets,
        check_build=dev_build if python and dev_build != service.build else None,
        test=["pytest", "-q"] if has_tests else ["npm", "test"] if npm_test else [],
        migrate=MIGRATE if migrates else [],
    )  # fmt: skip


def make_plan(
    product: Product,
    app: str,
    environment: str,
    release: int,
    images: dict[str, str],
    apps_domain: str,
) -> DeployPlan:
    """The whole plan for one release of the product."""
    manifest = product.manifest
    env = dict(manifest.env)
    if apps_domain:
        env["APP_URL"] = f"https://{app_host(app, environment, apps_domain)}"
    if manifest.storage is not None:
        env["STORAGE_DIR"] = "/data"  # the host mounts the app's files there
    try:
        services = [service_plan(s, product, images, env) for s in manifest.services]
        return DeployPlan(
            app=app,
            environment=environment,
            release=release,
            services=services,
            database=manifest.database is not None,
            storage_gb=manifest.storage.max_gb if manifest.storage else 0,
            limits=LIMITS[manifest.resource_class],
        )
    except ValidationError as err:
        problem = err.errors()[0]
        where = ".".join(str(part) for part in problem["loc"])
        raise PlanProblem(f"the product cannot be hosted: {where}: {problem['msg']}") from None
