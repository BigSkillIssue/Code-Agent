"""Apple's in-app purchase notifications, and what the apps' customers bought with them.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "apple_notifications",
        sa.Column("uuid", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("received_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "apple_entitlements",
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("original_transaction_id", sa.String(64), primary_key=True),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("product_id", sa.String(200), nullable=False),
        sa.Column("account_token", sa.String(64), nullable=False, server_default=""),
        sa.Column("expires_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("apple_entitlements")
    op.drop_table("apple_notifications")
