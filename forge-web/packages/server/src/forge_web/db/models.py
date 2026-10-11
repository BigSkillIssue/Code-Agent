"""Tables of the server database (SQLAlchemy 2, typed)."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class of every table."""


class User(Base):
    """A person who can sign in."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    avatar_url: Mapped[str] = mapped_column(String(1000), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | pending | disabled
    created_at: Mapped[float] = mapped_column(Float)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    default_model: Mapped[str] = mapped_column(String(200), default="")  # provider/model
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)  # no code is used twice
    recovery_codes: Mapped[str] = mapped_column(Text, default="")  # SHA-256 hashes, one per line
    apple_allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # Mac builds granted
    hosting_trusted: Mapped[bool] = mapped_column(Boolean, default=False)  # no admin approval


class Project(Base):
    """A folder of code with its own sandbox."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="empty")  # empty | git | zip | folder
    source_url: Mapped[str] = mapped_column(Text, default="")
    folder: Mapped[str] = mapped_column(Text, default="")  # server folder (admins only)
    kind: Mapped[str] = mapped_column(String(16), default="code")  # code | apple
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class ProjectMember(Base):
    """Who may use a project, and how."""

    __tablename__ = "project_members"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # owner | editor | viewer


class Chat(Base):
    """One conversation with Forge in a project."""

    __tablename__ = "chats"
    __table_args__ = (Index("chats_by_project", "project_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    state: Mapped[str] = mapped_column(String(16), default="idle")  # idle|running|waiting|stopped
    mode: Mapped[str] = mapped_column(String(16), default="edits")  # ask | edits | auto
    model: Mapped[str] = mapped_column(String(200), default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    daemon_boot: Mapped[str] = mapped_column(String(64), default="")
    token_generation: Mapped[int] = mapped_column(Integer, default=1)  # bump to revoke tokens
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class ChatEvent(Base):
    """One stored item of a chat's log, numbered from 1 without gaps."""

    __tablename__ = "chat_events"

    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    dseq: Mapped[int] = mapped_column(Integer, default=0)  # the daemon's number for it
    ts: Mapped[float] = mapped_column(Float)
    type: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), default="")  # event kind for type == event
    data: Mapped[str] = mapped_column(Text)  # the item as JSON


class ApiKey(Base):
    """A model provider's API key, encrypted: a user's own, or the server's (owner_id "")."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("api_keys_by_owner", "owner_id", "provider"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), default="")  # "" = a server key
    provider: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100), default="")
    hint: Mapped[str] = mapped_column(String(16), default="")  # the last characters, for people
    secret: Mapped[str] = mapped_column(Text)  # encrypted with the vault
    created_at: Mapped[float] = mapped_column(Float)
    last_used_at: Mapped[float] = mapped_column(Float, default=0)


class KeyGrant(Base):
    """A user's permission to use the server's keys, with a monthly limit."""

    __tablename__ = "key_grants"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = default


class UsageRecord(Base):
    """One model call through the gateway, as the upstream reported it (or estimated)."""

    __tablename__ = "usage"
    __table_args__ = (Index("usage_by_user", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[str] = mapped_column(String(32), default="")
    project_id: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200), default="")
    key_kind: Mapped[str] = mapped_column(String(8))  # own | server | none
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its hash is stored."""

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("auth_sessions_by_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class OneTimeToken(Base):
    """An invite, a password reset or an email check: a link that works once, for a while."""

    __tablename__ = "one_time_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the token
    purpose: Mapped[str] = mapped_column(String(16))  # invite | reset | verify
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AuditEntry(Base):
    """Something security-relevant that happened, and who did it."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("audit_by_time", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[float] = mapped_column(Float)
    user_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class Identity(Base):
    """A sign-in account at Google, GitHub or another provider, linked to a user."""

    __tablename__ = "identities"
    __table_args__ = (Index("identities_by_user", "user_id"),)

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)  # the provider's user id
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320), default="")
    username: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float)
    last_used_at: Mapped[float] = mapped_column(Float)


class GitCredential(Base):
    """A user's token for a git host (GitHub sign-in grant or a personal token), encrypted."""

    __tablename__ = "git_credentials"
    __table_args__ = (Index("git_credentials_by_user", "user_id", "host", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    host: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(200), default="")
    secret: Mapped[str] = mapped_column(Text)  # encrypted with the vault
    hint: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(16), default="token")  # oauth | token
    scopes: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[float] = mapped_column(Float)


class ServerSetting(Base):
    """A server setting an admin changed in the web UI (it wins over forge-web.toml)."""

    __tablename__ = "server_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. auth.signup
    value: Mapped[str] = mapped_column(Text)  # JSON
    updated_at: Mapped[float] = mapped_column(Float)
    updated_by: Mapped[str] = mapped_column(String(32), default="")


class MacWorker(Base):
    """A Mac that builds Apple apps for this server; it signs in with a token shown once."""

    __tablename__ = "mac_workers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 of the token's secret part
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[str] = mapped_column(String(40), default="")


class AppleJob(Base):
    """One build, test, archive or screenshot on a Mac, for a chat's sandbox."""

    __tablename__ = "apple_jobs"
    __table_args__ = (
        Index("apple_jobs_by_status", "status", "created_at"),
        Index("apple_jobs_by_user", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))  # build | screenshot
    params: Mapped[str] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed
    worker_id: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float] = mapped_column(Float, default=0.0)
    finished_at: Mapped[float] = mapped_column(Float, default=0.0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)  # Mac time, for the monthly limit
    outcome: Mapped[str] = mapped_column(Text, default="")  # a short summary for the admin page
    archive: Mapped[bool] = mapped_column(Boolean, default=False)  # an .xcarchive is kept


class AppleApproval(Base):
    """A user's "Ready for Apple" for an Apple project: who, when, and which commit (W21)."""

    __tablename__ = "apple_approvals"
    __table_args__ = (Index("apple_approvals_by_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(String(64))
    commit: Mapped[str] = mapped_column(String(64), default="")  # the project's HEAD then
    clean: Mapped[bool] = mapped_column(Boolean, default=False)  # no uncommitted changes then
    summary: Mapped[str] = mapped_column(Text, default="")  # the last product review's summary
    created_at: Mapped[float] = mapped_column(Float)


class AppStoreKey(Base):
    """A user's App Store Connect team key (W22): ids and the private key, encrypted."""

    __tablename__ = "appstore_keys"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key_id: Mapped[str] = mapped_column(String(32))
    issuer_id: Mapped[str] = mapped_column(String(64))
    team_id: Mapped[str] = mapped_column(String(32))
    secret: Mapped[str] = mapped_column(Text)  # the .p8 key, encrypted with the vault
    created_at: Mapped[float] = mapped_column(Float)
    checked_at: Mapped[float] = mapped_column(Float, default=0.0)
    check_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    check_message: Mapped[str] = mapped_column(Text, default="")


class AppleCertificate(Base):
    """A signing certificate Forge made for a user's team (W22b): its key and the certificate,
    encrypted with the vault."""

    __tablename__ = "apple_certificates"
    __table_args__ = (Index("apple_certificates_by_owner", "user_id", "team_id", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # Apple's id
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    team_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(40))  # DISTRIBUTION | MAC_INSTALLER_DISTRIBUTION
    secret: Mapped[str] = mapped_column(Text)  # {"key": PEM, "der": base64}, encrypted
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)


class AppleRelease(Base):
    """One approved commit on its way to TestFlight, for one platform (W22b): where it is, and
    what each step left (stored, so a restart goes on from there)."""

    __tablename__ = "apple_releases"
    __table_args__ = (
        Index("apple_releases_by_project", "project_id", "created_at"),
        Index("apple_releases_by_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approval_id: Mapped[str] = mapped_column(String(32))
    commit: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))  # ios (with iPad and Watch) | macos
    build_number: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # running | failed | done
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON: what the steps left
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class AppleListing(Base):
    """The App Store listing a user finished for an Apple project (W22c): what goes to Apple."""

    __tablename__ = "apple_listings"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[str] = mapped_column(Text, default="{}")  # Forge's StoreListing as JSON
    user_id: Mapped[str] = mapped_column(String(32), default="")  # who saved it last
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


class AppleSubmission(Base):
    """A release on its way through App Review to the App Store (W22d): prepared on the user's
    click, submitted only after the user confirmed it, released on another click."""

    __tablename__ = "apple_submissions"
    __table_args__ = (
        Index("apple_submissions_by_project", "project_id", "created_at"),
        Index("apple_submissions_by_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    release_id: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(16))
    version: Mapped[str] = mapped_column(String(32))  # e.g. 1.0
    status: Mapped[str] = mapped_column(String(16))  # preparing|ready|failed|submitted|released
    step: Mapped[str] = mapped_column(String(16))
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    contact: Mapped[str] = mapped_column(Text, default="{}")  # App Review's contact, JSON
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON: what the steps left
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
