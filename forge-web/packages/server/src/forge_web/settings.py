"""Server settings: defaults, then `forge-web.toml`, then `FORGE_WEB_<SECTION>__<KEY>` variables.

The config file lives in the data folder unless `--config` or `FORGE_WEB_CONFIG` names another.
"""

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ENV_PREFIX = "FORGE_WEB_"
RESERVED_ENV = frozenset({"FORGE_WEB_DATA_DIR", "FORGE_WEB_CONFIG"})
CONFIG_NAME = "forge-web.toml"


class SettingsError(ValueError):
    """The settings file or an override is invalid; the message says where."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(_Strict):
    """Where the server listens and how users reach it."""

    host: str = "127.0.0.1"
    port: int = 8420
    public_url: str = ""  # e.g. https://forge.example.com; empty = http://host:port


class DatabaseSettings(_Strict):
    """The server's own database (users, projects, chats); SQLite unless a URL is given."""

    url: str = ""  # e.g. postgresql+asyncpg://user@host/db; empty = SQLite in the data folder


class SandboxSettings(_Strict):
    """How projects are isolated from the host and from each other."""

    isolation: Literal["docker", "local"] = "docker"
    docker: str = "docker"  # the container CLI: docker or podman
    image: str = "forge-web-sandbox:latest"
    runtime: Literal["auto", "runc", "runsc"] = "auto"  # auto: gVisor (runsc) when installed
    cpus: float = Field(default=2.0, gt=0)
    memory: str = "4g"
    pids: int = Field(default=1024, ge=64)
    nofile: int = Field(default=8192, ge=256)
    idle_minutes: float = Field(default=30, gt=0)  # stop a project's container after this long


class DevSettings(_Strict):
    """Development mode: one local user, a login link in the log, optionally the fake model."""

    enabled: bool = False
    fake: bool = False  # every chat uses Forge's fake model (no API keys needed)
    fake_script: str = ""  # a FakeProvider script file instead of the built-in greeting


class WebSettings(_Strict):
    """Every setting of one Forge Web server."""

    data_dir: Path = Field(default_factory=lambda: default_data_dir())
    server: ServerSettings = ServerSettings()
    database: DatabaseSettings = DatabaseSettings()
    sandbox: SandboxSettings = SandboxSettings()
    dev: DevSettings = DevSettings()

    def base_url(self) -> str:
        """The URL users open, without a trailing slash."""
        if self.server.public_url:
            return self.server.public_url.rstrip("/")
        return f"http://{self.server.host}:{self.server.port}"

    def database_url(self) -> str:
        """The SQLAlchemy URL of the server database."""
        return self.database.url or f"sqlite+aiosqlite:///{self.data_dir / 'forge-web.db'}"


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """$FORGE_WEB_DATA_DIR, else the usual per-user data folder of this OS."""
    env = os.environ if environ is None else environ
    if env.get("FORGE_WEB_DATA_DIR"):
        return Path(env["FORGE_WEB_DATA_DIR"]).expanduser()
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "forge-web"


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The settings file: $FORGE_WEB_CONFIG, else forge-web.toml in the data folder."""
    env = os.environ if environ is None else environ
    if env.get("FORGE_WEB_CONFIG"):
        return Path(env["FORGE_WEB_CONFIG"]).expanduser()
    return default_data_dir(env) / CONFIG_NAME


def load_settings(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> WebSettings:
    """Defaults, then the file (if it exists), then environment, then `overrides` (dotted keys)."""
    env = os.environ if environ is None else environ
    file = path or config_path(env)
    data: dict[str, Any] = {"data_dir": str(default_data_dir(env))}
    if file.is_file():
        try:
            merge(data, tomllib.loads(file.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as err:
            raise SettingsError(f"{file}: {err}") from err
    merge(data, env_overrides(env))
    for key, value in (overrides or {}).items():
        merge(data, nested(key.split("."), value))
    try:
        return WebSettings.model_validate(data)
    except ValidationError as err:
        raise SettingsError(f"invalid settings (file {file}): {err}") from err


def env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """FORGE_WEB_SERVER__PORT=9000 -> {"server": {"port": "9000"}}; JSON values for lists."""
    found: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX) or name in RESERVED_ENV:
            continue
        keys = [part.lower() for part in name[len(ENV_PREFIX) :].split("__")]
        merge(found, nested(keys, parse_env_value(raw)))
    return found


def parse_env_value(raw: str) -> Any:
    """Lists and tables are written as JSON; everything else stays text for pydantic to coerce."""
    if raw.startswith(("[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def nested(keys: list[str], value: Any) -> dict[str, Any]:
    """["a", "b"], 1 -> {"a": {"b": 1}}."""
    result: Any = value
    for key in reversed(keys):
        result = {key: result}
    assert isinstance(result, dict)
    return result


def merge(base: dict[str, Any], extra: Mapping[str, Any]) -> None:
    """Merge `extra` into `base`: tables key by key, everything else replaced."""
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = dict(value) if isinstance(value, Mapping) else value
