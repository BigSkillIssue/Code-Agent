"""Two-factor sign-in, default models, and server settings changed in the web UI.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    """Add the columns and the table."""
    with op.batch_alter_table("users") as users:
        users.add_column(sa.Column("default_model", sa.String(200), nullable=False,
                                   server_default=""))  # fmt: skip
        users.add_column(sa.Column("totp_secret", sa.Text, nullable=True))
        users.add_column(sa.Column("totp_enabled", sa.Boolean, nullable=False,
                                   server_default=sa.false()))  # fmt: skip
        users.add_column(sa.Column("totp_last_step", sa.Integer, nullable=False,
                                   server_default="0"))  # fmt: skip
        users.add_column(sa.Column("recovery_codes", sa.Text, nullable=False, server_default=""))
    op.create_table(
        "server_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("server_settings")
    with op.batch_alter_table("users") as users:
        for column in ("recovery_codes", "totp_last_step", "totp_enabled", "totp_secret",
                       "default_model"):  # fmt: skip
            users.drop_column(column)
