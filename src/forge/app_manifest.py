"""The app manifest (S63): `forge.app.toml` describes a product Forge built for hosting.

Hosting reads only this file, never the product's Dockerfile or compose file. Secrets are listed
by name; their values live in Forge Web's vault and never in the product. Every problem in a
manifest is reported as a value, not only the first one.
"""

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MANIFEST = "forge.app.toml"
SLUG = r"^[a-z][a-z0-9-]{0,39}$"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECRET_LIKE = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|_KEY$|^KEY$")
RESERVED = ("PORT", "DATABASE_URL")  # hosting sets these for every service
SYNTAX_LINE = re.compile(r"at line (\d+)")

Runtime = Literal["python3.12", "node22", "static"]
ResourceClass = Literal["small", "medium", "large"]
Client = Literal["web", "apple", "android", "windows"]


def _web_only() -> list[Client]:
    """The clients of a product that names none: the web client."""
    return ["web"]


class _Strict(BaseModel):
    """Refuses keys the manifest does not know, so typos do not pass silently."""

    model_config = ConfigDict(extra="forbid")


class Service(_Strict):
    """One process of the product: an API, a worker or the built web client."""

    name: str = Field(pattern=SLUG)
    runtime: Runtime
    root: str = "."
    command: list[str] = Field(default_factory=list)
    build: list[str] = Field(default_factory=list)
    port: int = Field(ge=1024, le=65535)
    health: str = Field(default="/healthz", pattern=r"^/")
    route: str | None = Field(default=None, pattern=r"^/")


class Database(_Strict):
    """The product's own PostgreSQL database."""

    engine: Literal["postgres16"] = "postgres16"


class Storage(_Strict):
    """Files the product keeps for its users."""

    max_gb: int = Field(default=1, ge=1, le=100)


class Mail(_Strict):
    """Mail the product sends, always through Forge Web's relay."""

    daily_limit: int = Field(default=200, ge=1, le=10000)


class Payments(_Strict):
    """How the product takes money from its users."""

    kind: Literal["none", "relay"] = "none"
    digital_goods: bool = False


class AppManifest(_Strict):
    """Everything hosting needs to know about a product."""

    version: Literal[1] = 1
    name: str = Field(pattern=SLUG)
    resource_class: ResourceClass = "small"
    services: list[Service] = Field(min_length=1)
    database: Database | None = None
    storage: Storage | None = None
    mail: Mail | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    clients: list[Client] = Field(default_factory=_web_only)
    payments: Payments = Field(default_factory=Payments)


class ManifestProblem(BaseModel):
    """One thing wrong in a manifest, named by its field."""

    field: str
    message: str
    line: int | None = None


def load_manifest(path: Path) -> AppManifest | list[ManifestProblem]:
    """The manifest in a file or in a product folder, or its problems."""
    file = path / MANIFEST if path.is_dir() else path
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        return [ManifestProblem(field="", message=f"cannot read {MANIFEST}: {error.strerror}")]
    return parse_manifest(text)


def parse_manifest(text: str) -> AppManifest | list[ManifestProblem]:
    """The manifest in a TOML text, or every problem in it."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        found = SYNTAX_LINE.search(str(error))
        line = int(found.group(1)) if found else None
        return [ManifestProblem(field="", message=f"not valid TOML: {error}", line=line)]
    problems = _secret_values(data)
    try:
        manifest = AppManifest.model_validate(data)
    except ValidationError as error:
        problems += [_problem(item) for item in error.errors()]
        return _sorted(problems)
    problems += _service_problems(manifest) + _name_problems(manifest)
    return _sorted(problems) if problems else manifest


def _secret_values(data: dict[str, Any]) -> list[ManifestProblem]:
    """Secrets given with a value; such keys are taken out so the rest can still be checked."""
    problems: list[ManifestProblem] = []
    secrets = data.get("secrets")
    if isinstance(secrets, dict):
        for name in secrets:
            problems.append(_never_values(f"secrets.{name}", name))
        data["secrets"] = list(secrets)
    env = data.get("env")
    if isinstance(env, dict):
        for name in [name for name in env if SECRET_LIKE.search(name)]:
            problems.append(_never_values(f"env.{name}", name))
            del env[name]
    return problems


def _never_values(field: str, name: str) -> ManifestProblem:
    """The problem for a secret written into the manifest (its value is never repeated)."""
    message = (
        f"{name} looks like a secret: secrets lists names only, never values; "
        f'add "{name}" to secrets and set its value in Forge Web'
    )
    return ManifestProblem(field=field, message=message)


def _problem(error: Any) -> ManifestProblem:
    """A pydantic error as a problem with a dotted field path."""
    field = ".".join(str(part) for part in error["loc"])
    message = "unknown key" if error["type"] == "extra_forbidden" else str(error["msg"])
    return ManifestProblem(field=field, message=message)


def _service_problems(manifest: AppManifest) -> list[ManifestProblem]:
    """Names, ports and routes used twice, and services that cannot be started."""
    problems: list[ManifestProblem] = []
    names: dict[str, int] = {}
    ports: dict[int, str] = {}
    routes: dict[str, str] = {}
    for index, service in enumerate(manifest.services):
        at = f"services.{index}"
        if service.name in names:
            problems.append(ManifestProblem(field=f"{at}.name", message="name used twice"))
        names.setdefault(service.name, index)
        if service.port in ports:
            message = f"port {service.port} is already used by service '{ports[service.port]}'"
            problems.append(ManifestProblem(field=f"{at}.port", message=message))
        ports.setdefault(service.port, service.name)
        if service.route is not None and service.route in routes:
            message = f"route {service.route} is already served by '{routes[service.route]}'"
            problems.append(ManifestProblem(field=f"{at}.route", message=message))
        if service.route is not None:
            routes.setdefault(service.route, service.name)
        if service.runtime != "static" and not service.command:
            message = f"a {service.runtime} service needs a command to start it"
            problems.append(ManifestProblem(field=f"{at}.command", message=message))
    return problems


def _name_problems(manifest: AppManifest) -> list[ManifestProblem]:
    """Env and secret names that are malformed, reserved or given twice."""
    problems: list[ManifestProblem] = []
    for name in manifest.env:
        problems += _bad_name(f"env.{name}", name)
    for index, name in enumerate(manifest.secrets):
        found = _bad_name(f"secrets.{index}", name)
        if not found and name in manifest.env:
            found = [ManifestProblem(field=f"secrets.{index}", message=f"{name} is also in env")]
        problems += found
    return problems


def _bad_name(field: str, name: str) -> list[ManifestProblem]:
    """The problem with one variable name, if any."""
    if not ENV_NAME.match(name):
        return [ManifestProblem(field=field, message=f"{name}: use A-Z, 0-9 and _")]
    if name in RESERVED:
        return [ManifestProblem(field=field, message=f"{name} is set by hosting")]
    return []


def _sorted(problems: list[ManifestProblem]) -> list[ManifestProblem]:
    """Problems in a stable order: by field."""
    return sorted(problems, key=lambda problem: (problem.field, problem.message))
