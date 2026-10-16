"""Billing: dunning of unpaid bills.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "billing_dunning",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("opened_at", sa.Float, nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("stage_at", sa.Float, nullable=False),
        sa.Column("closed_at", sa.Float, nullable=False, server_default="0"),
    )  # fmt: skip
    op.create_index("billing_dunning_open", "billing_dunning", ["user_id", "closed_at"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("billing_dunning")
