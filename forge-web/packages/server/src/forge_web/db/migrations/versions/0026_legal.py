"""Legal texts, their acceptances, and checked VAT IDs.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    """Add the tables."""
    op.create_table(
        "legal_texts",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("published_at", sa.Float, nullable=False),
        sa.Column("published_by", sa.String(32), nullable=False, server_default=""),
    )
    op.create_table(
        "legal_acceptances",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("accepted_at", sa.Float, nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
    )  # fmt: skip
    op.create_index("legal_acceptances_by_user", "legal_acceptances",
                    ["user_id", "kind", "version"])  # fmt: skip
    op.create_table(
        "legal_vat_ids",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("vat_id", sa.String(20), nullable=False),
        sa.Column("valid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("address", sa.Text, nullable=False, server_default=""),
        sa.Column("checked_at", sa.Float, nullable=False),
    )  # fmt: skip


def downgrade() -> None:
    """Remove them again."""
    op.drop_table("legal_vat_ids")
    op.drop_table("legal_acceptances")
    op.drop_table("legal_texts")
