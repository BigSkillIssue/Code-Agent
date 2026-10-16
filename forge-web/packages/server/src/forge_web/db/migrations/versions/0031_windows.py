"""Windows workers and the Windows builds of the products.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "win_workers",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False, server_default="0"),
        sa.Column("version", sa.String(40), nullable=False, server_default=""),
    )
    op.create_table(
        "windows_builds",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("build_number", sa.Integer, nullable=False),
        sa.Column("version", sa.String(24), nullable=False),
        sa.Column("platform", sa.String(8), nullable=False, server_default="x64"),
        sa.Column("status", sa.String(16), nullable=False, server_default="packing"),
        sa.Column("worker_id", sa.String(16), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("log_tail", sa.Text, nullable=False, server_default=""),
        sa.Column("tests_passed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tests_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("file_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "build_number"),
    )  # fmt: skip
    op.create_index("windows_builds_by_status", "windows_builds", ["status", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_index("windows_builds_by_status", "windows_builds")
    op.drop_table("windows_builds")
    op.drop_table("win_workers")
