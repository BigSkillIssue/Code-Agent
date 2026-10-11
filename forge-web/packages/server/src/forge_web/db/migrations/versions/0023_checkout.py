"""Billing: Stripe's customers and the checkouts.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "billing_customers",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("stripe_customer", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "billing_checkouts",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("waiver_at", sa.Float, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("completed_at", sa.Float, nullable=False, server_default="0"),
    )  # fmt: skip
    op.create_index("billing_checkouts_by_user", "billing_checkouts", ["user_id", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("billing_checkouts")
    op.drop_table("billing_customers")
