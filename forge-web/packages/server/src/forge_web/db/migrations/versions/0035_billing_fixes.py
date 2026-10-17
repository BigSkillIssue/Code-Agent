"""Dunning follows the invoices that failed, and a trial is given once (W40a): the case's open
invoices, and the trial each checkout was offered.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"


def upgrade() -> None:
    """Add the columns."""
    with op.batch_alter_table("billing_dunning") as table:
        table.add_column(sa.Column("invoices", sa.Text, nullable=False, server_default=""))
    with op.batch_alter_table("billing_checkouts") as table:
        table.add_column(sa.Column("trial_days", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("billing_checkouts") as table:
        table.drop_column("trial_days")
    with op.batch_alter_table("billing_dunning") as table:
        table.drop_column("invoices")
