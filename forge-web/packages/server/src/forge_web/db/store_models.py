"""Tables of the app stores beyond Apple's (W37): Android builds and Google Play releases."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_web.db.models import Base


class AndroidBuild(Base):
    """One Android build of an approved commit on the app's host: its version, how it went, and
    the unsigned bundle it made (kept on this server)."""

    __tablename__ = "android_builds"
    __table_args__ = (UniqueConstraint("project_id", "version_code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    commit: Mapped[str] = mapped_column(String(64))  # the newest "Ready to go live" then
    version_code: Mapped[int] = mapped_column(Integer)  # higher for every build of the project
    version_name: Mapped[str] = mapped_column(String(40))
    package_name: Mapped[str] = mapped_column(String(200), default="")  # its applicationId
    job_id: Mapped[str] = mapped_column(String(32), default="")  # set before the job is sent
    status: Mapped[str] = mapped_column(String(16), default="building")  # built, failed
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    log_tail: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)  # the bundle's bytes
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class PlayKey(Base):
    """A user's Google Play service account (W37b): its email, its key file sealed by the vault,
    and which kind of developer account it publishes for (personal or organisation)."""

    __tablename__ = "play_keys"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    client_email: Mapped[str] = mapped_column(String(320))
    secret: Mapped[str] = mapped_column(Text)
    account: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float)
    checked_at: Mapped[float] = mapped_column(Float, default=0.0)
    check_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    check_message: Mapped[str] = mapped_column(Text, default="")


class PlayUploadKey(Base):
    """An app's upload key for Google Play, made once by Forge Web (the key sealed)."""

    __tablename__ = "play_upload_keys"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    secret: Mapped[str] = mapped_column(Text)
    certificate: Mapped[str] = mapped_column(Text)  # PEM, for Play Console
    created_at: Mapped[float] = mapped_column(Float)


class PlayRelease(Base):
    """One build on its way to a Google Play track: who started it, the admin's go, and how it
    went (waiting, releasing, released, draft, failed)."""

    __tablename__ = "play_releases"
    __table_args__ = (Index("play_releases_by_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    build_id: Mapped[str] = mapped_column(ForeignKey("android_builds.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    track: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16))
    admin: Mapped[str] = mapped_column(String(16), default="")  # approved, trusted, admin
    admin_id: Mapped[str] = mapped_column(String(32), default="")
    admin_approved_at: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class WinWorker(Base):
    """A Windows machine that tests and packages Windows apps for this server (W38a); it signs in
    with a token shown once."""

    __tablename__ = "win_workers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 of the token's secret part
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[str] = mapped_column(String(40), default="")


class WindowsBuild(Base):
    """One Windows build of an approved commit, which is also the worker's job: packed, waiting,
    running on a worker, then built (its package kept here) or failed."""

    __tablename__ = "windows_builds"
    __table_args__ = (
        UniqueConstraint("project_id", "build_number"),
        Index("windows_builds_by_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    commit: Mapped[str] = mapped_column(String(64))  # the newest "Ready to go live" then
    build_number: Mapped[int] = mapped_column(Integer)  # higher for every build of the project
    version: Mapped[str] = mapped_column(String(24))  # the package's, 1.0.<build number>.0
    platform: Mapped[str] = mapped_column(String(8), default="x64")
    status: Mapped[str] = mapped_column(String(16), default="packing")
    worker_id: Mapped[str] = mapped_column(String(16), default="")
    claimed_at: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    log_tail: Mapped[str] = mapped_column(Text, default="")
    tests_passed: Mapped[int] = mapped_column(Integer, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
