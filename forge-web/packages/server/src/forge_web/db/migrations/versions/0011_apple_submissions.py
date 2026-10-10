"""Submissions to the App Store: each release's way through App Review.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "apple_submissions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("release_id", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("contact", sa.Text, nullable=False, server_default="{}"),
        sa.Column("data", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("apple_submissions_by_project", "apple_submissions",
                    ["project_id", "created_at"])  # fmt: skip
    op.create_index("apple_submissions_by_status", "apple_submissions", ["status"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("apple_submissions")
