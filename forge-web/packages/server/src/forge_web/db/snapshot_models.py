"""Tables of the snapshots after every run (W66): the snapshots and each project's key."""

from sqlalchemy import BigInteger, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_web.db.models import Base


class ProjectSnapshot(Base):
    """One sealed git bundle of a project's state after a run: a full one, or an incremental one
    that needs its parent (and the parent's chain) to be read back."""

    __tablename__ = "project_snapshots"
    __table_args__ = (Index("project_snapshots_by_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # also the blob's id
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(32), default="")  # the run that ended
    ref: Mapped[str] = mapped_column(String(80), default="")  # refs/forge-web/snapshots/<n>
    commit: Mapped[str] = mapped_column(String(64), default="")
    tree: Mapped[str] = mapped_column(String(64), default="")
    parent_id: Mapped[str | None] = mapped_column(String(32), default=None)  # incremental only
    kind: Mapped[str] = mapped_column(String(8), default="full")  # full | incr
    size: Mapped[int] = mapped_column(BigInteger, default=0)  # of the sealed file
    sha256: Mapped[str] = mapped_column(String(64), default="")  # of the sealed file
    node_id: Mapped[str | None] = mapped_column(String(16), default=None)  # None: this server
    state: Mapped[str] = mapped_column(String(16))  # pending | stored | failed | deleted
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float)


class ProjectSnapshotKey(Base):
    """The key a project's snapshots are sealed with (32 bytes, kept encrypted by the vault)."""

    __tablename__ = "project_snapshot_keys"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
