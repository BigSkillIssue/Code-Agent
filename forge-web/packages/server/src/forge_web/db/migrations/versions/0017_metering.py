"""Metering and alerts of hosted apps: usage samples, incidents, a host's free disk.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    """Add the tables and the column."""
    with op.batch_alter_table("host_servers") as hosts:
        hosts.add_column(sa.Column("disk_free_gb", sa.Float, nullable=False, server_default="0"))
    op.create_table(
        "app_usage",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("at", sa.Float, nullable=False),
        sa.Column("containers", sa.Integer, nullable=False),
        sa.Column("running", sa.Integer, nullable=False),
        sa.Column("cpu_percent", sa.Float, nullable=False),
        sa.Column("memory_mb", sa.Float, nullable=False),
        sa.Column("net_rx_mb", sa.Float, nullable=False),
        sa.Column("net_tx_mb", sa.Float, nullable=False),
        sa.Column("disk_mb", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("app_usage_by_app", "app_usage", ["project_id", "environment", "at"])
    op.create_table(
        "hosting_incidents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("opened_at", sa.Float, nullable=False),
        sa.Column("closed_at", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("incidents_open", "hosting_incidents", ["key", "closed_at"])


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("hosting_incidents")
    op.drop_table("app_usage")
    with op.batch_alter_table("host_servers") as hosts:
        hosts.drop_column("disk_free_gb")
