"""A cancellation from the open form can be taken back from the account's mailbox (security
review after W39): the link's token hash, the subscriptions it ended, and when it was undone.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"


def upgrade() -> None:
    """Add the columns."""
    with op.batch_alter_table("billing_cancellations") as table:
        table.add_column(sa.Column("undo_hash", sa.String(64), nullable=False, server_default=""))
        table.add_column(sa.Column("subscriptions", sa.Text, nullable=False, server_default=""))
        table.add_column(sa.Column("undone_at", sa.Float, nullable=False, server_default="0"))


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("billing_cancellations") as table:
        table.drop_column("undone_at")
        table.drop_column("subscriptions")
        table.drop_column("undo_hash")
