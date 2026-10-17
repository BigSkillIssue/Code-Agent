"""The admin controls every customer's billing (W40c): plans given for free, and dunning cases
held by an admin.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"


def upgrade() -> None:
    """Add the table and the column."""
    op.create_table(
        "billing_plan_gifts",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("until", sa.Float, nullable=False, server_default="0"),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("granted_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("granted_at", sa.Float, nullable=False),
    )  # fmt: skip
    with op.batch_alter_table("billing_dunning") as table:
        table.add_column(sa.Column("held_at", sa.Float, nullable=False, server_default="0"))


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("billing_dunning") as table:
        table.drop_column("held_at")
    op.drop_table("billing_plan_gifts")
