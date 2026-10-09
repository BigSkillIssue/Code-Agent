"""Apple apps: Mac workers, build jobs, who may build, and the kind of a project.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    """Add the tables and the columns."""
    with op.batch_alter_table("users") as users:
        users.add_column(sa.Column("apple_allowed", sa.Boolean, nullable=False,
                                   server_default=sa.false()))  # fmt: skip
    with op.batch_alter_table("projects") as projects:
        projects.add_column(sa.Column("kind", sa.String(16), nullable=False,
                                      server_default="code"))  # fmt: skip
    op.create_table(
        "mac_workers",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False, server_default="0"),
        sa.Column("version", sa.String(40), nullable=False, server_default=""),
    )
    op.create_table(
        "apple_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("params", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("finished_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("seconds", sa.Float, nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text, nullable=False, server_default=""),
        sa.Column("archive", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("apple_jobs_by_status", "apple_jobs", ["status", "created_at"])
    op.create_index("apple_jobs_by_user", "apple_jobs", ["user_id", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_index("apple_jobs_by_user", "apple_jobs")
    op.drop_index("apple_jobs_by_status", "apple_jobs")
    op.drop_table("apple_jobs")
    op.drop_table("mac_workers")
    with op.batch_alter_table("projects") as projects:
        projects.drop_column("kind")
    with op.batch_alter_table("users") as users:
        users.drop_column("apple_allowed")
