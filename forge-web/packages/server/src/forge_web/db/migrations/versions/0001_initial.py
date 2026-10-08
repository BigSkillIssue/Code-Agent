"""Users, projects, members, chats and chat events.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    """Create the first tables."""
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(320), unique=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="empty"),
        sa.Column("source_url", sa.Text, nullable=False, server_default=""),
        sa.Column("folder", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "project_members",
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
    )
    op.create_table(
        "chats",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="New chat"),
        sa.Column("state", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="edits"),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("shared", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("daemon_boot", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("chats_by_project", "chats", ["project_id", "updated_at"])
    op.create_table(
        "chat_events",
        sa.Column(
            "chat_id",
            sa.String(32),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer, primary_key=True),
        sa.Column("dseq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("data", sa.Text, nullable=False),
    )


def downgrade() -> None:
    """Drop them again."""
    for table in ("chat_events", "chats", "project_members", "projects", "users"):
        op.drop_table(table)
