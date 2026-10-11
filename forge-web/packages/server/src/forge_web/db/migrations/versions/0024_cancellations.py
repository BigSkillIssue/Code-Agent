"""Billing: cancellations through the cancellation button.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "billing_cancellations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("received_at", sa.Float, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("contract", sa.String(200), nullable=False, server_default=""),
        sa.Column("extraordinary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("user_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("effective_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("sender", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("billing_cancellations_by_sender", "billing_cancellations",
                    ["sender", "received_at"])  # fmt: skip


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("billing_cancellations")
