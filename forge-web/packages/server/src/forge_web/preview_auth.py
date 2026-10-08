"""Who may open which preview: preview hosts, one-time tickets and the cookie a ticket becomes.

Every preview lives on a host of its own, `p<port>-<project>.<domain>`, so the app in it is
another origin than Forge (and, with a domain of its own, another site): it never gets Forge's
cookies and cannot call Forge's API as the user. The UI asks the API for a ticket (one minute,
one use, bound to the user's session, the project and the port) and opens the preview with it;
the preview host turns the ticket into a signed cookie for that host only.
"""

import re
import secrets
import time
from dataclasses import dataclass, field

from forge_web.access import project_role
from forge_web.db.engine import Database
from forge_web.db.models import AuthSession, User
from forge_web.settings import WebSettings
from forge_web.vault import sign

ENTER_PATH = "/__forge_preview/enter"
COOKIE_NAME = "__Host-forge_preview"
TICKET_SECONDS = 60.0
COOKIE_SECONDS = 12 * 3600
RECHECK_SECONDS = 30.0  # how long a positive membership check is trusted
MAX_TICKETS = 10_000
LABEL = re.compile(r"^p([1-9][0-9]{0,4})-([a-z0-9][a-z0-9-]{0,47})$")
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class PreviewBase:
    """Where previews live: scheme, parent domain and the port in their URLs."""

    scheme: str
    domain: str
    port: int | None

    def host(self, project_id: str, port: int) -> str:
        """The host of one preview, as browsers address it."""
        suffix = f":{self.port}" if self.port else ""
        return f"p{port}-{project_id}.{self.domain}{suffix}"

    def url(self, project_id: str, port: int, path: str = "/") -> str:
        """A URL on one preview's host."""
        return f"{self.scheme}://{self.host(project_id, port)}{path}"

    def frame_source(self) -> str:
        """Every preview host, as a CSP source (Forge's pages may frame these)."""
        suffix = f":{self.port}" if self.port else ""
        return f"{self.scheme}://*.{self.domain}{suffix}"


@dataclass(frozen=True)
class Target:
    """The preview a request is for."""

    project_id: str
    port: int
    origin: str  # scheme://host[:port] as the browser addressed it


def preview_base(settings: WebSettings) -> PreviewBase | None:
    """Where previews live; None when they are off (no domain on a server others can reach)."""
    chosen = settings.preview
    if chosen.domain:
        https = chosen.https
        if https is None:
            https = settings.base_url().startswith("https://")
        return PreviewBase("https" if https else "http", chosen.domain.lower(), chosen.port)
    if settings.server.host in LOOPBACK:  # a local install: browsers resolve *.localhost
        return PreviewBase("http", "localhost", chosen.port or settings.server.port)
    return None


def preview_target(base: PreviewBase, host: str | None) -> Target | None:
    """The preview a Host header names, or None for any other host."""
    if not host:
        return None
    host = host.lower()
    label, dot, parent = host.split(":", 1)[0].partition(".")
    found = LABEL.match(label)
    if not dot or parent != base.domain or found is None:
        return None
    port = int(found.group(1))
    if port > 65535:
        return None
    return Target(found.group(2), port, f"{base.scheme}://{host}")


@dataclass(frozen=True)
class Grant:
    """What a ticket or a preview cookie allows: one user's session, one project, one port."""

    user_id: str
    session_id: str
    project_id: str
    port: int
    expires: float


@dataclass
class PreviewAccess:
    """Tickets handed out by the API, and the cookies they turn into."""

    key: bytes
    tickets: dict[str, Grant] = field(default_factory=dict)
    checked: dict[str, float] = field(default_factory=dict)  # cookie value -> last good check

    def issue(
        self, user_id: str, session_id: str, project_id: str, port: int, now: float | None = None
    ) -> str:
        """A new one-time ticket for this preview."""
        now = time.time() if now is None else now
        self.tickets = {t: g for t, g in self.tickets.items() if g.expires > now}
        if len(self.tickets) >= MAX_TICKETS:
            self.tickets.pop(next(iter(self.tickets)))
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = Grant(user_id, session_id, project_id, port, now + TICKET_SECONDS)
        return ticket

    def redeem(self, ticket: str, target: Target, now: float | None = None) -> Grant | None:
        """A cookie's grant if the ticket is fresh and for this preview; used up either way."""
        now = time.time() if now is None else now
        found = self.tickets.pop(ticket, None)
        if found is None or found.expires < now or not matches(found, target):
            return None
        return Grant(found.user_id, found.session_id, found.project_id, found.port,
                     float(int(now + COOKIE_SECONDS)))  # fmt: skip

    def cookie_value(self, grant: Grant) -> str:
        """The signed cookie value for a grant."""
        payload = ".".join(
            (grant.user_id, grant.session_id, grant.project_id, str(grant.port),
             str(int(grant.expires)))
        )  # fmt: skip
        return f"{payload}.{sign(self.key, payload)}"

    def read_cookie(self, value: str, target: Target, now: float | None = None) -> Grant | None:
        """The grant in a cookie we signed, if it is unexpired and for this preview."""
        now = time.time() if now is None else now
        payload, _, signature = value.rpartition(".")
        if not payload or not secrets.compare_digest(sign(self.key, payload), signature):
            return None
        parts = payload.split(".")
        if len(parts) != 5 or not parts[3].isdigit() or not parts[4].isdigit():
            return None
        grant = Grant(parts[0], parts[1], parts[2], int(parts[3]), float(parts[4]))
        return grant if grant.expires > now and matches(grant, target) else None

    async def still_allowed(self, db: Database, value: str, grant: Grant) -> bool:
        """The session is live and its user still a member (checked again every 30 s)."""
        now = time.monotonic()
        if now - self.checked.get(value, -RECHECK_SECONDS) < RECHECK_SECONDS:
            return True
        async with db.session() as session:
            row = await session.get(AuthSession, grant.session_id)
            user = await session.get(User, grant.user_id)
            ok = (
                row is not None and row.user_id == grant.user_id and row.expires_at > time.time()
                and user is not None and user.status == "active"
                and await project_role(session, user, grant.project_id) is not None
            )  # fmt: skip
        if ok:
            if len(self.checked) >= MAX_TICKETS:
                self.checked.clear()
            self.checked[value] = now
        else:
            self.checked.pop(value, None)
        return ok

    def forget_checks(self) -> None:
        """Check every cookie against the database again on its next use."""
        self.checked.clear()


def matches(grant: Grant, target: Target) -> bool:
    """The grant is for the preview the request goes to."""
    return grant.project_id == target.project_id and grant.port == target.port
