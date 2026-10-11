"""Hosting administration: an admin can suspend a hosted app (with the reason).

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    """Add the columns."""
    with op.batch_alter_table("hosted_apps") as apps:
        apps.add_column(
            sa.Column("suspended", sa.Boolean, nullable=False, server_default=sa.false())
        )
        apps.add_column(sa.Column("suspended_reason", sa.Text, nullable=False, server_default=""))
        apps.add_column(sa.Column("suspended_at", sa.Float, nullable=False, server_default="0"))


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("hosted_apps") as apps:
        for column in ("suspended_at", "suspended_reason", "suspended"):
            apps.drop_column(column)
