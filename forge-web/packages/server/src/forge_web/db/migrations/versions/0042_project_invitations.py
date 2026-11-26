"""Project invitations (SEC-W1): adding someone to a project, or making a member an owner, only
offers them the role; they take it by accepting. Members from before count as accepted.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "project_invitations",
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("invited_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("project_invitations_by_user", "project_invitations", ["user_id"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_index("project_invitations_by_user", "project_invitations")
    op.drop_table("project_invitations")
