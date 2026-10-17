"""An app's one-time price unlocks that app (W40b): the unlocked apps, and the project and the
payment of each order.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"


def upgrade() -> None:
    """Add the table and the columns."""
    op.create_table(
        "billing_app_licenses",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("checkout_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("granted_at", sa.Float, nullable=False),
        sa.Column("granted_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("revoked_reason", sa.String(200), nullable=False, server_default=""),
    )  # fmt: skip
    op.create_index("billing_app_licenses_by_project", "billing_app_licenses",
                    ["project_id", "revoked_at"])  # fmt: skip
    with op.batch_alter_table("billing_checkouts") as table:
        table.add_column(sa.Column("project_id", sa.String(64), nullable=False, server_default=""))
        table.add_column(sa.Column("payment_intent", sa.String(255), nullable=False,
                                   server_default=""))  # fmt: skip


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("billing_checkouts") as table:
        table.drop_column("payment_intent")
        table.drop_column("project_id")
    op.drop_index("billing_app_licenses_by_project", "billing_app_licenses")
    op.drop_table("billing_app_licenses")
