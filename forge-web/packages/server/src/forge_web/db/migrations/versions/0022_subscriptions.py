"""Billing: what customers pay for.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False, server_default="month"),
        sa.Column("stripe_subscription", sa.String(255), nullable=False, server_default=""),
        sa.Column("current_period_end", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("billing_subscriptions_by_user", "billing_subscriptions", ["user_id", "status"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("billing_subscriptions")
