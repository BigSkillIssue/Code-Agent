"""App Store Connect: each user's team key, encrypted.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "appstore_keys",
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("issuer_id", sa.String(64), nullable=False),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("check_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("check_message", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("appstore_keys")
