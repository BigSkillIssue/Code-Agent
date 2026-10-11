"""Tables of hosting (W26): the hosts, the apps on them, approvals, secrets, deploys, jobs."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_web.db.models import Base


class HostServer(Base):
    """A Linux server that runs hosted apps (`forge-host-worker`); it signs in with a token
    shown once, and secrets are sealed to its public key."""

    __tablename__ = "host_servers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 of the token's secret part
    public_key: Mapped[str] = mapped_column(String(100))  # X25519, base64
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[str] = mapped_column(String(40), default="")


class HostedApp(Base):
    """A project's name on the hosts (unique on this server, it is also its host name)."""

    __tablename__ = "hosted_apps"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    app: Mapped[str] = mapped_column(String(40), unique=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("host_servers.id"))
    created_at: Mapped[float] = mapped_column(Float)


class GoLiveApproval(Base):
    """A user's "Ready to go live" for an app project: who, when, and which commit (S67b)."""

    __tablename__ = "go_live_approvals"
    __table_args__ = (Index("go_live_by_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(String(64))
    commit: Mapped[str] = mapped_column(String(64), default="")  # the project's HEAD then
    clean: Mapped[bool] = mapped_column(Boolean, default=False)  # no uncommitted changes then
    summary: Mapped[str] = mapped_column(Text, default="")  # the release review's summary
    created_at: Mapped[float] = mapped_column(Float)


class AppSecret(Base):
    """One secret value of a hosted app in one environment (encrypted with the vault)."""

    __tablename__ = "app_secrets"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    environment: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # encrypted
    updated_at: Mapped[float] = mapped_column(Float)


class Deploy(Base):
    """One deploy of an approved commit to staging or production, step by step (W26)."""

    __tablename__ = "deploys"
    __table_args__ = (
        Index("deploys_by_project", "project_id", "created_at"),
        Index("deploys_by_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))  # the creator
    app: Mapped[str] = mapped_column(String(40))
    host_id: Mapped[str] = mapped_column(ForeignKey("host_servers.id"))
    environment: Mapped[str] = mapped_column(String(16))  # staging | production
    commit: Mapped[str] = mapped_column(String(64))
    release: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(16))  # see forge_web.hosting.deploy.STEPS
    status: Mapped[str] = mapped_column(String(16))  # running | waiting | live | failed | ...
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    creator_approved_at: Mapped[float] = mapped_column(Float, default=0.0)
    admin_id: Mapped[str] = mapped_column(String(32), default="")  # who approved as admin
    admin_approved_at: Mapped[float] = mapped_column(Float, default=0.0)
    migrated: Mapped[bool] = mapped_column(Boolean, default=False)  # never twice
    data: Mapped[str] = mapped_column(Text, default="{}")  # the plan, checks, job ids
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class HostJobRow(Base):
    """A job for a host; it waits here until the host takes it, and keeps its result."""

    __tablename__ = "host_jobs"
    __table_args__ = (Index("host_jobs_by_host", "host_id", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("host_servers.id"))
    deploy_id: Mapped[str] = mapped_column(String(32), default="")
    app: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    job: Mapped[str] = mapped_column(Text)  # the HostJob as JSON
    status: Mapped[str] = mapped_column(String(16))  # queued | running | done | lost
    sealed: Mapped[str] = mapped_column(Text, default="")  # SealedSecrets, until fetched once
    result: Mapped[str] = mapped_column(Text, default="")  # the JobResult as JSON
    created_at: Mapped[float] = mapped_column(Float)
    claimed_at: Mapped[float] = mapped_column(Float, default=0.0)
    finished_at: Mapped[float] = mapped_column(Float, default=0.0)
