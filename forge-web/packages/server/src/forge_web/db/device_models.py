"""Device time Forge's agent used (W60): one row per session, for the users' monthly minutes."""

from sqlalchemy import Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from forge_web.db.models import Base


class DeviceUsage(Base):
    """One device session: whose, on which platform, from when to when."""

    __tablename__ = "device_usage"
    __table_args__ = (Index("device_usage_by_user", "user_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[float] = mapped_column(Float)
    ended_at: Mapped[float] = mapped_column(Float)
