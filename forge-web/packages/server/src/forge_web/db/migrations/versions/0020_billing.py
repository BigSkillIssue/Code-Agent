"""Billing: the admin's Stripe keys and Stripe's events.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "billing_keys",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("secret_key", sa.Text, nullable=False),
        sa.Column("webhook_secret", sa.Text, nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("hint", sa.String(8), nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=False, server_default=""),
    )
    op.create_table(
        "billing_events",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("object_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("data", sa.Text, nullable=False),
        sa.Column("received_at", sa.Float, nullable=False),
        sa.Column("handled_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text, nullable=False, server_default=""),
    )
    op.create_index("billing_events_waiting", "billing_events", ["handled_at", "created"])
    op.create_index("billing_events_by_object", "billing_events", ["object_id", "created"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("billing_events")
    op.drop_table("billing_keys")
