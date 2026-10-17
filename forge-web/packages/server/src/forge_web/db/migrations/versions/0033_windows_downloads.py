"""Windows apps on the products' websites instead of the Microsoft Store (W39): signing
machines, the builds' Velopack releases, the releases on the websites; the Store's tables go.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"


def upgrade() -> None:
    """Drop the Store's tables, add the releases."""
    op.drop_index("msstore_submissions_by_project", "msstore_submissions")
    op.drop_table("msstore_submissions")
    op.drop_table("msstore_keys")
    with op.batch_alter_table("win_workers") as table:
        table.add_column(sa.Column("signs", sa.Boolean, nullable=False, server_default=sa.false()))
    with op.batch_alter_table("windows_builds") as table:
        table.add_column(sa.Column("pack_id", sa.String(40), nullable=False, server_default=""))
        table.add_column(sa.Column("signed", sa.Boolean, nullable=False,
                                   server_default=sa.false()))  # fmt: skip
        table.add_column(sa.Column("files", sa.Text, nullable=False, server_default=""))
    op.create_table(
        "windows_releases",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("build_id", sa.String(32),
                  sa.ForeignKey("windows_builds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("admin", sa.String(16), nullable=False, server_default=""),
        sa.Column("admin_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("admin_approved_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("published_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("withdrawn_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("windows_releases_by_project", "windows_releases",
                    ["project_id", "created_at"])  # fmt: skip


def downgrade() -> None:
    """The releases go; the Store's tables come back empty."""
    op.drop_index("windows_releases_by_project", "windows_releases")
    op.drop_table("windows_releases")
    with op.batch_alter_table("windows_builds") as table:
        table.drop_column("files")
        table.drop_column("signed")
        table.drop_column("pack_id")
    with op.batch_alter_table("win_workers") as table:
        table.drop_column("signs")
    op.create_table(
        "msstore_keys",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("check_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("check_message", sa.Text, nullable=False, server_default=""),
    )  # fmt: skip
    op.create_table(
        "msstore_submissions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("build_id", sa.String(32),
                  sa.ForeignKey("windows_builds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("store_id", sa.String(12), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("submission_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("store_status", sa.String(40), nullable=False, server_default=""),
        sa.Column("followed_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("admin", sa.String(16), nullable=False, server_default=""),
        sa.Column("admin_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("admin_approved_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("msstore_submissions_by_project", "msstore_submissions",
                    ["project_id", "created_at"])  # fmt: skip
