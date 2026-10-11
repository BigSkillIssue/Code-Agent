"""Hosting: the hosts, the apps on them, "Ready to go live", app secrets, deploys and jobs.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"


def project_column() -> sa.Column[str]:
    """The project a row belongs to (gone with the project)."""
    return sa.Column(
        "project_id",
        sa.String(32),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    """Add the tables and the user's "trusted for hosting"."""
    with op.batch_alter_table("users") as users:
        users.add_column(
            sa.Column("hosting_trusted", sa.Boolean, nullable=False, server_default=sa.false())
        )
    op.create_table(
        "host_servers",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("public_key", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False, server_default="0"),
        sa.Column("version", sa.String(40), nullable=False, server_default=""),
    )
    op.create_table(
        "hosted_apps",
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("app", sa.String(40), nullable=False, unique=True),
        sa.Column("host_id", sa.String(16), sa.ForeignKey("host_servers.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "go_live_approvals",
        sa.Column("id", sa.String(32), primary_key=True),
        project_column(),
        sa.Column("chat_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False, server_default=""),
        sa.Column("clean", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("go_live_by_project", "go_live_approvals", ["project_id", "created_at"])
    op.create_table(
        "app_secrets",
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("environment", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_table(
        "deploys",
        sa.Column("id", sa.String(32), primary_key=True),
        project_column(),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("app", sa.String(40), nullable=False),
        sa.Column("host_id", sa.String(16), sa.ForeignKey("host_servers.id"), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("release", sa.Integer, nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        sa.Column("creator_approved_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("admin_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("admin_approved_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("migrated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("data", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("deploys_by_project", "deploys", ["project_id", "created_at"])
    op.create_index("deploys_by_status", "deploys", ["status"])
    op.create_table(
        "host_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("host_id", sa.String(16), sa.ForeignKey("host_servers.id"), nullable=False),
        sa.Column("deploy_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("app", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("job", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sealed", sa.Text, nullable=False, server_default=""),
        sa.Column("result", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("claimed_at", sa.Float, nullable=False, server_default="0"),
        sa.Column("finished_at", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("host_jobs_by_host", "host_jobs", ["host_id", "status", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    for table in ("host_jobs", "deploys", "app_secrets", "go_live_approvals", "hosted_apps",
                  "host_servers"):  # fmt: skip
        op.drop_table(table)
    with op.batch_alter_table("users") as users:
        users.drop_column("hosting_trusted")
