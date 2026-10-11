"""Notices of illegal content in hosted apps (DSA Art. 16) and the admins' decisions.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "abuse_notices",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("app", sa.String(40), nullable=False),
        sa.Column("location", sa.Text, nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("sender", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision", sa.Text, nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("decided_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("abuse_notices_by_status", "abuse_notices", ["status", "created_at"])
    op.create_index("abuse_notices_by_sender", "abuse_notices", ["sender", "created_at"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("abuse_notices")
