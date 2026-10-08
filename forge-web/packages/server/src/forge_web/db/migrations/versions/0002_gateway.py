"""API keys, grants, usage records, and token generations for chats.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    """Create the gateway tables."""
    with op.batch_alter_table("chats") as batch:
        batch.add_column(
            sa.Column("token_generation", sa.Integer, nullable=False, server_default="1")
        )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False, server_default=""),
        sa.Column("hint", sa.String(16), nullable=False, server_default=""),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_used_at", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("api_keys_by_owner", "api_keys", ["owner_id", "provider"])
    op.create_table(
        "key_grants",
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("allowed", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("monthly_limit_usd", sa.Float, nullable=True),
    )
    op.create_table(
        "usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("chat_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("key_kind", sa.String(8), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("estimated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("usage_by_user", "usage", ["user_id", "created_at"])


def downgrade() -> None:
    """Drop them again."""
    for table in ("usage", "key_grants", "api_keys"):
        op.drop_table(table)
    with op.batch_alter_table("chats") as batch:
        batch.drop_column("token_generation")
