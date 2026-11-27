"""Snapshots after every run (W66): the sealed bundles of a project's state and its key.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "project_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("ref", sa.String(80), nullable=False, server_default=""),
        sa.Column("commit", sa.String(64), nullable=False, server_default=""),
        sa.Column("tree", sa.String(64), nullable=False, server_default=""),
        sa.Column("parent_id", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(8), nullable=False, server_default="full"),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("node_id", sa.String(16), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("project_snapshots_by_project", "project_snapshots",
                    ["project_id", "created_at"])  # fmt: skip
    op.create_table(
        "project_snapshot_keys",
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("project_snapshot_keys")
    op.drop_index("project_snapshots_by_project", "project_snapshots")
    op.drop_table("project_snapshots")
