"""Apple apps: the users' approvals ("Ready for Apple") of a project's state.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    """Add the table."""
    op.create_table(
        "apple_approvals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False, server_default=""),
        sa.Column("clean", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("apple_approvals_by_project", "apple_approvals", ["project_id", "created_at"])


def downgrade() -> None:
    """Remove it again."""
    op.drop_index("apple_approvals_by_project", "apple_approvals")
    op.drop_table("apple_approvals")
