"""SqliteStore: sessions in ~/.forge/forge.db (SQLAlchemy async + aiosqlite), FTS5 for recall."""

import re
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from forge.local.memory_store import excerpt, session_texts
from forge.ports import Session, SessionNotFoundError

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        project_root TEXT NOT NULL,
        created_at REAL NOT NULL,
        status TEXT NOT NULL,
        data TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS sessions_by_project ON sessions (project_root, created_at)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS search_index
        USING fts5(session_id UNINDEXED, project_root UNINDEXED, body)""",
    """CREATE TABLE IF NOT EXISTS board (
        session_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        owner TEXT,
        claimed_at REAL,
        PRIMARY KEY (session_id, step_id)
    )""",
]
_WORD = re.compile(r"\w+", re.UNICODE)


def _fast_commits(dbapi_connection: Any, _record: Any) -> None:
    """WAL with synchronous=NORMAL: safe against crashes of Forge, far fewer fsyncs per commit."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


class SqliteStore:
    """A Store backed by one SQLite file; the full session is kept as JSON."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
        event.listen(self.engine.sync_engine, "connect", _fast_commits)
        self._ready = False

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self.engine.begin() as conn:
            for statement in SCHEMA:
                await conn.execute(text(statement))
        self._ready = True

    async def create_session(self, project_root: str) -> Session:
        """Create and save a new active session."""
        session = Session(
            id=uuid.uuid4().hex, project_root=project_root, created_at=time.time(), status="active"
        )
        await self.save_session(session)
        return session

    async def save_session(self, s: Session) -> None:
        """Insert or replace the session and refresh its search entries, in one transaction."""
        await self._setup()
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO sessions (id, project_root, created_at, status, data) "
                    "VALUES (:id, :root, :created, :status, :data)"
                ),
                {
                    "id": s.id,
                    "root": s.project_root,
                    "created": s.created_at,
                    "status": s.status,
                    "data": s.model_dump_json(),
                },
            )
            await conn.execute(
                text("DELETE FROM search_index WHERE session_id = :id"), {"id": s.id}
            )
            for body in session_texts(s):
                await conn.execute(
                    text(
                        "INSERT INTO search_index (session_id, project_root, body) "
                        "VALUES (:id, :root, :body)"
                    ),
                    {"id": s.id, "root": s.project_root, "body": body},
                )

    async def load_session(self, session_id: str) -> Session:
        """The stored session; SessionNotFoundError if unknown."""
        await self._setup()
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT data FROM sessions WHERE id = :id"), {"id": session_id}
                )
            ).first()
        if row is None:
            raise SessionNotFoundError(session_id)
        return Session.model_validate_json(row[0])

    async def list_sessions(self, project_root: str, limit: int = 20) -> list[Session]:
        """A project's sessions, newest first."""
        await self._setup()
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT data FROM sessions WHERE project_root = :root "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"root": project_root, "limit": limit},
            )
            return [Session.model_validate_json(row[0]) for row in rows]

    async def search(self, project_root: str, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """Sessions whose texts contain every query word (FTS5), best match first."""
        words = _WORD.findall(query)
        if not words:
            return []
        match = " ".join('"' + w.replace('"', "") + '"' for w in words)
        await self._setup()
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT search_index.session_id, body FROM search_index "
                    "JOIN sessions ON sessions.id = search_index.session_id "
                    "WHERE search_index MATCH :match AND search_index.project_root = :root "
                    "ORDER BY rank, sessions.created_at DESC"
                ),
                {"match": match, "root": project_root},
            )
            hits: list[tuple[str, str]] = []
            for session_id, body in rows:
                if session_id not in {h[0] for h in hits}:
                    hits.append((session_id, excerpt(body, words[0])))
        return hits[:limit]

    async def claim(self, session_id: str, step_id: str, owner: str) -> str:
        """Set the owner if the step has none, in one statement; returns the owner after it."""
        await self._setup()
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO board (session_id, step_id, owner, claimed_at) "
                    "VALUES (:sid, :step, :owner, :now) "
                    "ON CONFLICT (session_id, step_id) DO UPDATE "
                    "SET owner = excluded.owner, claimed_at = excluded.claimed_at "
                    "WHERE board.owner IS NULL"
                ),
                {"sid": session_id, "step": step_id, "owner": owner, "now": time.time()},
            )
            row = await conn.execute(
                text("SELECT owner FROM board WHERE session_id = :sid AND step_id = :step"),
                {"sid": session_id, "step": step_id},
            )
            return str(row.scalar_one())

    async def release(self, session_id: str, step_id: str) -> None:
        """Clear the step's owner."""
        await self._setup()
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE board SET owner = NULL WHERE session_id = :sid AND step_id = :step"),
                {"sid": session_id, "step": step_id},
            )

    async def owners(self, session_id: str) -> dict[str, str]:
        """step id -> owner for every owned step."""
        await self._setup()
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT step_id, owner FROM board WHERE session_id = :sid AND owner IS NOT NULL"
                ),
                {"sid": session_id},
            )
            return {str(step): str(owner) for step, owner in rows}

    async def close(self) -> None:
        """Release the database connection pool."""
        await self.engine.dispose()
