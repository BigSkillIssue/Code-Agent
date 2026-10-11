"""Backups of hosted apps: each app's backup key and its encrypted backups with their drills.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "app_backup_keys",
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "app_backups",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("drill_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("drill_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("drill_detail", sa.Text, nullable=False, server_default=""),
    )  # fmt: skip
    op.create_index(
        "app_backups_by_app", "app_backups", ["project_id", "environment", "created_at"]
    )


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("app_backups")
    op.drop_table("app_backup_keys")
