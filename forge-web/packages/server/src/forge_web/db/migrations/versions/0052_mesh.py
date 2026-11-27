"""Forge's WireGuard network (W65): no two nodes share an address (nodes without one may be many).

Revision ID: 0052
Revises: 0043
"""

from alembic import op

revision = "0052"
down_revision = "0043"


def upgrade() -> None:
    """Make the nodes' addresses unique."""
    op.create_index("host_mesh_ip", "host_servers", ["mesh_ip"], unique=True)


def downgrade() -> None:
    """Drop the index again."""
    op.drop_index("host_mesh_ip", table_name="host_servers")
