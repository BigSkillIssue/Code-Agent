"""Device time of Forge's agent (W60): one row per device session.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "device_usage",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("ended_at", sa.Float, nullable=False),
    )
    op.create_index("device_usage_by_user", "device_usage", ["user_id", "started_at"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_index("device_usage_by_user", "device_usage")
    op.drop_table("device_usage")
