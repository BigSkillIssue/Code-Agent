"""Passwords, sessions, one-time links and the audit log.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    """Create the account tables."""
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(200), nullable=True))
        batch.add_column(
            sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.false())
        )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("csrf", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("auth_sessions_by_user", "auth_sessions", ["user_id"])
    op.create_table(
        "one_time_tokens",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=True),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("created_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("used_at", sa.Float, nullable=True),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False, server_default=""),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("audit_by_time", "audit_log", ["created_at"])


def downgrade() -> None:
    """Drop them again."""
    for table in ("audit_log", "one_time_tokens", "auth_sessions"):
        op.drop_table(table)
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified")
        batch.drop_column("password_hash")
