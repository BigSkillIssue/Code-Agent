"""Sign-in identities (Google, GitHub, OpenID Connect) and git credentials.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def user_fk() -> sa.ForeignKey:
    """A reference to the user (each table needs its own)."""
    return sa.ForeignKey("users.id", ondelete="CASCADE")


def upgrade() -> None:
    """Create the tables."""
    op.create_table(
        "identities",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(32), user_fk(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("username", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_used_at", sa.Float, nullable=False),
    )
    op.create_index("identities_by_user", "identities", ["user_id"])
    op.create_table(
        "git_credentials",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), user_fk(), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("username", sa.String(200), nullable=False, server_default=""),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("hint", sa.String(16), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="token"),
        sa.Column("scopes", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("git_credentials_by_user", "git_credentials", ["user_id", "host"], unique=True)


def downgrade() -> None:
    """Drop them again."""
    op.drop_table("git_credentials")
    op.drop_table("identities")
