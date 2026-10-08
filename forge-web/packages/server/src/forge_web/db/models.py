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


class Project(Base):
    """A folder of code with its own sandbox."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="empty")  # empty | git | zip | folder
    source_url: Mapped[str] = mapped_column(Text, default="")
    folder: Mapped[str] = mapped_column(Text, default="")  # server folder (admins only)
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
