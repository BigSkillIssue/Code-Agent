"""Customers' own domains for hosted apps.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "app_domains",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("verified_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("checked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
    )  # fmt: skip
    op.create_index("app_domains_by_project", "app_domains", ["project_id"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_table("app_domains")
