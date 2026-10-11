"""Deleting hosted apps: when a deletion began, and why it did not finish yet.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    """Add the columns."""
    with op.batch_alter_table("hosted_apps") as apps:
        apps.add_column(sa.Column("deleting_at", sa.Float, nullable=False, server_default="0"))
        apps.add_column(sa.Column("deleting_error", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    """Drop them."""
    with op.batch_alter_table("hosted_apps") as apps:
        apps.drop_column("deleting_error")
        apps.drop_column("deleting_at")
