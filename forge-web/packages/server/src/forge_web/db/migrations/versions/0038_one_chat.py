"""One chat per project (W44): the user asked for a single conversation per project that every
member sees. Of a project's chats the one used last stays and the others go, with their items;
the one left runs on the project's account (its owner of record) and remembers who started its
current run.

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"


def upgrade() -> None:
    """Keep each project's newest chat, then allow only one."""
    chats = sa.table(
        "chats",
        sa.column("id", sa.String),
        sa.column("project_id", sa.String),
        sa.column("updated_at", sa.Float),
    )
    rows = op.get_bind().execute(
        sa.select(chats.c.id, chats.c.project_id).order_by(
            chats.c.project_id, chats.c.updated_at.desc(), chats.c.id
        )
    )
    seen: set[str] = set()
    extra: list[str] = []
    for chat_id, project_id in rows:  # newest first within each project
        if project_id in seen:
            extra.append(chat_id)
        seen.add(project_id)
    events = sa.table("chat_events", sa.column("chat_id", sa.String))
    for start in range(0, len(extra), 500):
        batch = extra[start : start + 500]
        op.execute(events.delete().where(events.c.chat_id.in_(batch)))
        op.execute(chats.delete().where(chats.c.id.in_(batch)))
    op.execute(
        "UPDATE chats SET shared = true, user_id = "
        "(SELECT owner_id FROM projects WHERE projects.id = chats.project_id)"
    )
    with op.batch_alter_table("chats") as table:
        table.add_column(sa.Column("started_by", sa.String(32), nullable=False, server_default=""))
        table.drop_index("chats_by_project")
        table.create_index("chats_one_per_project", ["project_id"], unique=True)


def downgrade() -> None:
    """Allow several chats again (the deleted ones do not come back)."""
    with op.batch_alter_table("chats") as table:
        table.drop_index("chats_one_per_project")
        table.create_index("chats_by_project", ["project_id", "updated_at"])
        table.drop_column("started_by")
