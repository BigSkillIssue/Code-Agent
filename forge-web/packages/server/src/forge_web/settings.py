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
    max_upload_mb: int = Field(default=200, ge=1)  # largest file one upload may bring


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
    # Folders on the server that admins may open as projects (empty = not allowed at all).
    folder_roots: list[str] = []


DEFAULT_EGRESS = [
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com",
    "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "gitlab.com", "bitbucket.org", "crates.io", "static.crates.io",
    "index.crates.io", "proxy.golang.org", "sum.golang.org", "rubygems.org",
    "repo.maven.apache.org", "deb.debian.org", "security.debian.org",
    "developer.apple.com",  # Apple's guidelines, read by the Apple reviewer
]  # fmt: skip


class GatewaySettings(_Strict):
    """The model gateway: whose keys pay, and how much."""

    server_keys_for: Literal["admins", "granted", "everyone"] = "granted"
    monthly_limit_usd: float = Field(default=20.0, ge=0)  # per user, on the server's keys
    max_output_tokens: int = Field(default=64_000, ge=256)  # per request
    # How long a chat's model calls may go on after its last message or answer.
    run_minutes: float = Field(default=120, gt=0)
    upstreams: dict[str, str] = {}  # provider -> base URL (self-hosted proxies, tests)


class EgressSettings(_Strict):
    """Where programs in a container may connect (through the egress proxy)."""

    enabled: bool = True
    allow: list[str] = Field(default_factory=lambda: list(DEFAULT_EGRESS))  # "*" = any host
    allow_private: bool = False  # private and loopback addresses (only for tests)


class QuotaSettings(_Strict):
    """Limits per user and per project (0 = no limit); admins have no project limit."""

    projects_per_user: int = Field(default=20, ge=0)
    project_disk_mb: int = Field(default=10_000, ge=0)
    chat_log_mb: int = Field(default=1_000, ge=0)  # stored chat history per project
    disk_check_minutes: float = Field(default=5, gt=0)  # how often disk use is measured outside


class GitSettings(_Strict):
    """Pushing and pulling: which hosts git jobs may reach."""

    hosts: list[str] = ["*"]  # git hosts (and their subdomains); "*" = any public host
    timeout_s: float = Field(default=300, gt=0)
    # Development and tests only (needs dev mode): file:// remotes and private addresses.
    allow_local_remotes: bool = False


class PreviewSettings(_Strict):
    """Live previews of dev servers: each on its own host, `p<port>-<project>.<domain>`.

    The domain needs wildcard DNS to this server and should be a registrable domain of its own
    (not a subdomain of Forge's), so previews are other sites. Without a domain, a server that
    listens only on loopback serves previews on `*.localhost` (browsers resolve those locally).
    """

    domain: str = ""  # e.g. preview.example.net
    https: bool | None = None  # None: like public_url
    port: int | None = Field(default=None, ge=1, le=65535)  # in preview URLs; None: the default


class SmtpSettings(_Strict):
    """Outgoing mail for verification and reset links (optional)."""

    host: str = ""  # empty = no mail; admins hand out links instead
    port: int = 587
    username: str = ""
    password_env: str = "FORGE_WEB_SMTP_PASSWORD"  # the password is read from this variable
    from_address: str = ""
    starttls: bool = True


class ProviderSettings(_Strict):
    """A sign-in provider: Google, GitHub, or any OpenID Connect issuer (Microsoft, GitLab,
    Keycloak, …). The client secret is read from `client_secret_env`, else `client_secret`."""

    kind: Literal["google", "github", "oidc"] | None = None  # None: from the name, else oidc
    label: str = ""  # the button text; empty = from the name
    client_id: str
    client_secret_env: str = ""  # empty = FORGE_WEB_<NAME>_SECRET
    client_secret: str = ""
    issuer: str = ""  # oidc: the issuer URL (its /.well-known/openid-configuration is read)
    url: str = ""  # github: GitHub Enterprise's address; empty = github.com
    scopes: list[str] = []  # extra scopes
    trust_email: bool = False  # oidc: the issuer checks emails but sends no email_verified

    def provider_kind(self, name: str) -> str:
        """google, github or oidc."""
        return self.kind or (name if name in ("google", "github") else "oidc")


class AuthSettings(_Strict):
    """Who may sign up and how sign-in works."""

    signup: Literal["invite", "approval", "open"] = "invite"
    allowed_domains: list[str] = []  # for approval/open sign-up: only these email domains
    passwords: bool = True  # email + password accounts
    session_days: float = Field(default=30, gt=0)
    admin_two_factor: bool = False  # admins must sign in with a second factor
    allowed_origins: list[str] = []  # extra origins for the browser (besides public_url)
    smtp: SmtpSettings = SmtpSettings()
    providers: dict[str, ProviderSettings] = {}  # name -> provider, e.g. google, github


class AppleSettings(_Strict):
    """Apple apps: builds, tests and screenshots on Macs that connect to this server
    (`forge-mac-worker`); each project builds in a macOS VM of its own."""

    enabled: bool = False
    allowed: Literal["admins", "granted", "everyone"] = "granted"  # who may build
    minutes_per_month: float = Field(default=600, ge=0)  # Mac time per user; 0 = no limit
    max_source_mb: int = Field(default=300, ge=1)  # the packed project of one job
    max_archive_mb: int = Field(default=4_096, ge=1)  # an .xcarchive coming back
    job_timeout_s: float = Field(default=2_400, gt=0)  # waiting for a Mac included
    keep_archives_days: float = Field(default=14, gt=0)
    # The guideline reviewer's model ("provider/model"); empty: the chat's model.
    reviewer_model: str = Field(default="", pattern=r"^$|^[\w.-]+/[\w.:@/-]+$")
    # Apple's App Store Connect API (W22); tests point it at a stand-in on this machine.
    asc_api_url: str = Field(
        default="https://api.appstoreconnect.apple.com",
        pattern=r"^https://[\w.-]+(:\d+)?$|^http://127\.0\.0\.1:\d+$",
    )
    release_poll_s: float = Field(default=30, gt=0)  # asking Apple about a build's processing
    processing_timeout_s: float = Field(default=3 * 3600, gt=0)  # Apple's processing, at most
    review_poll_s: float = Field(default=600, gt=0)  # asking App Review how far it is

    def allows(self, role: str, granted: bool) -> bool:
        """Whether a user with this role (and grant) may build Apple apps on this server."""
        if not self.enabled:
            return False
        return (
            self.allowed == "everyone" or role == "admin" or (self.allowed == "granted" and granted)
        )


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
    auth: AuthSettings = AuthSettings()
    gateway: GatewaySettings = GatewaySettings()
    egress: EgressSettings = EgressSettings()
    git: GitSettings = GitSettings()
    quotas: QuotaSettings = QuotaSettings()
    preview: PreviewSettings = PreviewSettings()
    apple: AppleSettings = AppleSettings()
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
        # Only FORGE_WEB_<SECTION>__<KEY> names are settings: secrets such as
        # FORGE_WEB_SMTP_PASSWORD share the prefix but are read where they are needed.
        if not name.startswith(ENV_PREFIX) or name in RESERVED_ENV or "__" not in name:
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
