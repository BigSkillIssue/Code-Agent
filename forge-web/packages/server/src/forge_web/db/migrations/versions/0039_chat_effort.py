"""How hard a chat's model thinks (W45): auto, low, medium or high, as Forge's `effort`.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"


def upgrade() -> None:
    """Add the column."""
    with op.batch_alter_table("chats") as table:
        table.add_column(sa.Column("effort", sa.String(16), nullable=False, server_default="auto"))


def downgrade() -> None:
    """Remove it again."""
    with op.batch_alter_table("chats") as table:
        table.drop_column("effort")
