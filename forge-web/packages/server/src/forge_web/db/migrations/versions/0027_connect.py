"""Makers' connected Stripe accounts, the apps' payments tokens and the checkouts they opened.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "connect_accounts",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("stripe_account", sa.String(64), nullable=False, unique=True),
        sa.Column("charges_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("payouts_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("details_submitted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "app_payments",
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("environment", sa.String(16), primary_key=True),
        sa.Column("token", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "relay_checkouts",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("stripe_account", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fee", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reference", sa.String(200), nullable=False, server_default=""),
        sa.Column("subscription", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("relay_checkouts_by_app", "relay_checkouts",
                    ["project_id", "environment", "subscription"])  # fmt: skip


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("relay_checkouts")
    op.drop_table("app_payments")
    op.drop_table("connect_accounts")
