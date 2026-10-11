"""Billing: the catalog and its coupons.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "billing_items",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False, server_default="eur"),
        sa.Column("amounts", sa.Text, nullable=False, server_default="{}"),
        sa.Column("trial_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("limits", sa.Text, nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("archived_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("stripe_product", sa.String(255), nullable=False, server_default=""),
        sa.Column("stripe_prices", sa.Text, nullable=False, server_default="{}"),
        sa.Column("product_synced", sa.String(32), nullable=False, server_default=""),
        sa.Column("synced_at", sa.Float, nullable=False, server_default="0"),
    )
    op.create_table(
        "billing_coupons",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("percent_off", sa.Float, nullable=True),
        sa.Column("amount_off", sa.Integer, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="eur"),
        sa.Column("duration", sa.String(16), nullable=False, server_default="once"),
        sa.Column("duration_in_months", sa.Integer, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("archived_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("stripe_coupon", sa.String(255), nullable=False, server_default=""),
        sa.Column("stripe_promotion", sa.String(255), nullable=False, server_default=""),
        sa.Column("synced_at", sa.Float, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("billing_coupons")
    op.drop_table("billing_items")
