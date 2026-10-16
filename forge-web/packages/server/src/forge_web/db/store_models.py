"""Tables of the app stores beyond Apple's (W37): the Android builds of a product."""

from sqlalchemy import BigInteger, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
