"""JsonStore: sessions as JSON files in a folder — a second Store, written against ports only.

It exists to show that a new storage backend needs no change in the core: one file per
session (`sessions/<id>.json`) and one file for the team board (`board.json`).
"""

import asyncio
import json
import time
import uuid
from pathlib import Path

from forge.local.memory_store import rank_sessions
from forge.ports import Session, SessionNotFoundError


class JsonStore:
    """A Store (and BoardStore) backed by plain JSON files."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        (folder / "sessions").mkdir(parents=True, exist_ok=True)
        self._board_lock = asyncio.Lock()

    def _path(self, session_id: str) -> Path:
        return self.folder / "sessions" / f"{session_id}.json"

    async def create_session(self, project_root: str) -> Session:
        """Create and save a new active session."""
        session = Session(
            id=uuid.uuid4().hex, project_root=project_root, created_at=time.time(), status="active"
        )
        await self.save_session(session)
        return session

    async def save_session(self, s: Session) -> None:
        """Write the session file atomically (temp file, then rename)."""
        path = self._path(s.id)
        temp = path.with_suffix(".tmp")
        await asyncio.to_thread(temp.write_text, s.model_dump_json(), "utf-8")
        await asyncio.to_thread(temp.replace, path)

    async def load_session(self, session_id: str) -> Session:
        """Read one session."""
        path = self._path(session_id)
        if not path.is_file():
            raise SessionNotFoundError(session_id)
        return Session.model_validate_json(await asyncio.to_thread(path.read_text, "utf-8"))

    async def _all(self) -> list[Session]:
        paths = sorted((self.folder / "sessions").glob("*.json"))
        texts = [await asyncio.to_thread(p.read_text, "utf-8") for p in paths]
        return [Session.model_validate_json(t) for t in texts]

    async def list_sessions(self, project_root: str, limit: int = 20) -> list[Session]:
        """A project's sessions, newest first."""
        found = [s for s in await self._all() if s.project_root == project_root]
        found.sort(key=lambda s: s.created_at, reverse=True)
        return found[:limit]

    async def search(self, project_root: str, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """Sessions whose texts contain every query word, best match first."""
        return rank_sessions(await self._all(), project_root, query, limit)

    # ------------------------------------------------------------------ BoardStore

    def _board(self) -> dict[str, str]:
        path = self.folder / "board.json"
        return dict(json.loads(path.read_text("utf-8"))) if path.is_file() else {}

    def _write_board(self, board: dict[str, str]) -> None:
        (self.folder / "board.json").write_text(json.dumps(board), "utf-8")

    async def claim(self, session_id: str, step_id: str, owner: str) -> str:
        """Set the owner if the step has none (one claim at a time, under a lock)."""
        async with self._board_lock:
            board = self._board()
            key = f"{session_id}/{step_id}"
            board.setdefault(key, owner)
            self._write_board(board)
            return board[key]

    async def release(self, session_id: str, step_id: str) -> None:
        """Clear the step's owner."""
        async with self._board_lock:
            board = self._board()
            board.pop(f"{session_id}/{step_id}", None)
            self._write_board(board)

    async def owners(self, session_id: str) -> dict[str, str]:
        """step id -> owner."""
        prefix = f"{session_id}/"
        return {k.removeprefix(prefix): v for k, v in self._board().items() if k.startswith(prefix)}
