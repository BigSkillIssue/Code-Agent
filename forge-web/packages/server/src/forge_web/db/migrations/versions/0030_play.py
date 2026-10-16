"""Google Play: the users' service accounts, the apps' upload keys and the releases.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "play_keys",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("client_email", sa.String(320), nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("account", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("check_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("check_message", sa.Text, nullable=False, server_default=""),
    )  # fmt: skip
    op.create_table(
        "play_upload_keys",
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("certificate", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "play_releases",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("build_id", sa.String(32),
                  sa.ForeignKey("android_builds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("track", sa.String(16), nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("admin", sa.String(16), nullable=False, server_default=""),
        sa.Column("admin_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("admin_approved_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("play_releases_by_project", "play_releases", ["project_id", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_index("play_releases_by_project", "play_releases")
    op.drop_table("play_releases")
    op.drop_table("play_upload_keys")
    op.drop_table("play_keys")
