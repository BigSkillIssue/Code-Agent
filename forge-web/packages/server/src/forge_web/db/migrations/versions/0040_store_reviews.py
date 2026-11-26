"""Store reviews by Forge's store-review agent (W58): which commit, which store, the verdict.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "store_reviews",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.String(32), nullable=False),
        sa.Column("store", sa.String(16), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("findings", sa.Text, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("store_reviews_by_project", "store_reviews",
                    ["project_id", "store", "created_at"])  # fmt: skip


def downgrade() -> None:
    """Remove it again."""
    op.drop_index("store_reviews_by_project", "store_reviews")
    op.drop_table("store_reviews")
