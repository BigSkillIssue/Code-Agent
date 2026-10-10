"""The App Store listing of each Apple project, as its user finished it.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "apple_listings",
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", sa.Text, nullable=False, server_default="{}"),
        sa.Column("user_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Float, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("apple_listings")
