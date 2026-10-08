"""Connecting to the database and bringing its schema up to date."""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

MIGRATIONS = Path(__file__).parent / "migrations"


class Database:
    """The engine and a session factory for one database URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        """A new session (use as `async with db.session() as s`)."""
        return self.sessions()

    async def migrate(self) -> None:
        """Apply every migration that has not run yet."""
        async with self.engine.begin() as connection:
            await connection.run_sync(_upgrade)

    async def close(self) -> None:
        """Close every pooled connection."""
        await self.engine.dispose()


def _upgrade(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    # WAL lets readers work while the writer task commits; foreign keys are off by default.
    cursor = dbapi_connection.cursor()
    for pragma in (
        "journal_mode=WAL",
        "busy_timeout=5000",
        "foreign_keys=ON",
        "synchronous=NORMAL",
    ):
        cursor.execute(f"PRAGMA {pragma}")
    cursor.close()
