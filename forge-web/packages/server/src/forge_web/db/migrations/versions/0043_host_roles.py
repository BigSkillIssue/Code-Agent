"""Server roles and join codes (W63): a host runs apps, databases, AI models or storage (hosts
from before run apps), has an address in Forge's WireGuard network, and a new server joins with
a one-time code instead of a token pasted by hand.

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"


def upgrade() -> None:
    """Add the columns and the table."""
    with op.batch_alter_table("host_servers") as table:
        table.add_column(sa.Column("roles", sa.Text, nullable=False, server_default='["apps"]'))
        table.add_column(sa.Column("mesh_ip", sa.String(45), nullable=True))
        table.add_column(sa.Column("wg_public_key", sa.String(64), nullable=True))
        table.add_column(sa.Column("endpoint", sa.String(300), nullable=True))
        table.add_column(sa.Column("gpu", sa.Text, nullable=True))
        table.add_column(sa.Column("models", sa.Text, nullable=True))
    op.create_table(
        "host_join_codes",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("roles", sa.Text, nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("used_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("host_id", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("host_join_codes")
    with op.batch_alter_table("host_servers") as table:
        for column in ("models", "gpu", "endpoint", "wg_public_key", "mesh_ip", "roles"):
            table.drop_column(column)
