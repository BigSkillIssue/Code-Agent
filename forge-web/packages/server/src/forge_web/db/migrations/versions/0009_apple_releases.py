"""Releases to TestFlight: signing certificates per user and team, and each release's steps.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "apple_certificates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("apple_certificates_by_owner", "apple_certificates",
                    ["user_id", "team_id", "kind"])  # fmt: skip
    op.create_table(
        "apple_releases",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approval_id", sa.String(32), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("build_number", sa.Integer, nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("data", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("apple_releases_by_project", "apple_releases", ["project_id", "created_at"])
    op.create_index("apple_releases_by_status", "apple_releases", ["status"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("apple_releases")
    op.drop_table("apple_certificates")
