"""The trial and the guided start (W72): a new account's trial with its dates, credit and mails,
and whether the guided start is still due for an account (accounts from before it skip it).

Revision ID: 0051
Revises: 0052
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0052"


def upgrade() -> None:
    """Add the table and the column."""
    op.create_table(
        "trials",
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("ends_at", sa.Float, nullable=False),
        sa.Column("credit_usd", sa.Float, nullable=False),
        sa.Column("reminded_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("ended_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("ended_reason", sa.String(16), nullable=False, server_default=""),
        sa.Column("apps_handed_at", sa.Float, nullable=False, server_default="0"),
    )  # fmt: skip
    with op.batch_alter_table("users") as table:
        table.add_column(
            sa.Column("onboarding_due", sa.Boolean, nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Remove them again."""
    with op.batch_alter_table("users") as table:
        table.drop_column("onboarding_due")
    op.drop_table("trials")
