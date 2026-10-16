"""The Android builds of the products.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "android_builds",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("version_code", sa.Integer, nullable=False),
        sa.Column("version_name", sa.String(40), nullable=False),
        sa.Column("package_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("job_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="building"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("log_tail", sa.Text, nullable=False, server_default=""),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "version_code"),
    )  # fmt: skip


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("android_builds")
