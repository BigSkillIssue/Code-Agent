"""App services: who operates each app (Impressum), its mail relay and what it sent.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"


def project_key() -> sa.Column[str]:
    """The project a row belongs to, as (part of) its key."""
    return sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                     primary_key=True)  # fmt: skip


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "app_operators",
        project_key(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("representative", sa.String(300), nullable=False, server_default=""),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("phone", sa.String(60), nullable=False, server_default=""),
        sa.Column("trade_register", sa.String(300), nullable=False, server_default=""),
        sa.Column("vat_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("privacy_email", sa.String(320), nullable=False, server_default=""),
        sa.Column("privacy_extra", sa.Text, nullable=False, server_default=""),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_table(
        "app_mail",
        project_key(),
        sa.Column("environment", sa.String(16), primary_key=True),
        sa.Column("token", sa.Text, nullable=False),
        sa.Column("daily_limit", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "app_mail_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )  # fmt: skip
    op.create_index("app_mail_by_day", "app_mail_log", ["project_id", "environment", "created_at"])


def downgrade() -> None:
    """Remove them again."""
    for table in ("app_mail_log", "app_mail", "app_operators"):
        op.drop_table(table)
