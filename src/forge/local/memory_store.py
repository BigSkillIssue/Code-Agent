"""A dict-backed Store, used in tests and until the SQLite store exists."""

import re
import time
import uuid
from collections.abc import Iterable

from forge.ports import Session, SessionNotFoundError

EXCERPT_CHARS = 400


class MemoryStore:
    """Keeps sessions in memory; every save and load copies, like a real database would."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._board: dict[tuple[str, str], str] = {}

    async def create_session(self, project_root: str) -> Session:
        """Create and save a new active session."""
        session = Session(
            id=uuid.uuid4().hex,
            project_root=project_root,
            created_at=time.time(),
            status="active",
        )
        await self.save_session(session)
        return session

    async def save_session(self, s: Session) -> None:
        """Store a copy of the session."""
        self._sessions[s.id] = s.model_copy(deep=True)

    async def load_session(self, session_id: str) -> Session:
        """Return a copy of a stored session."""
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)
        return self._sessions[session_id].model_copy(deep=True)

    async def list_sessions(self, project_root: str, limit: int = 20) -> list[Session]:
        """A project's sessions, newest first."""
        found = [s for s in self._sessions.values() if s.project_root == project_root]
        found.sort(key=lambda s: s.created_at, reverse=True)
        return [s.model_copy(deep=True) for s in found[:limit]]

    async def claim(self, session_id: str, step_id: str, owner: str) -> str:
        """Set the owner if the step has none (no await in between, so it is atomic)."""
        return self._board.setdefault((session_id, step_id), owner)

    async def release(self, session_id: str, step_id: str) -> None:
        """Clear the step's owner."""
        self._board.pop((session_id, step_id), None)

    async def owners(self, session_id: str) -> dict[str, str]:
        """step id -> owner."""
        return {step: owner for (sid, step), owner in self._board.items() if sid == session_id}

    async def search(self, project_root: str, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """Sessions whose texts contain every query word, best match first."""
        return rank_sessions(self._sessions.values(), project_root, query, limit)


def rank_sessions(
    sessions: Iterable[Session], project_root: str, query: str, limit: int
) -> list[tuple[str, str]]:
    """(id, excerpt) of a project's sessions containing every query word, most matches first."""
    words = [w.lower() for w in re.findall(r"\w+", query)]
    if not words:
        return []
    scored: list[tuple[int, float, str, str]] = []
    for session in sessions:
        if session.project_root != project_root:
            continue
        hits = [t for t in session_texts(session) if all(w in t.lower() for w in words)]
        if hits:
            score = sum(t.lower().count(w) for t in hits for w in words)
            scored.append((score, session.created_at, session.id, excerpt(hits[0], words[0])))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [(sid, text) for _, _, sid, text in scored[:limit]]


def session_texts(session: Session) -> list[str]:
    """The searchable texts of a session: summary, plan steps, then messages (newest first)."""
    texts = [session.summary] if session.summary else []
    if session.plan:
        texts += [f"{step.title}\n{step.notes}".strip() for step in session.plan.steps]
    texts += [m.text() for m in reversed(session.messages) if m.text()]
    return texts


def excerpt(text: str, word: str, size: int = EXCERPT_CHARS) -> str:
    """Up to `size` characters of `text`, centred on the first occurrence of `word`."""
    if len(text) <= size:
        return text
    centre = max(text.lower().find(word.lower()), 0)
    start = max(0, min(centre - size // 2, len(text) - size))
    snippet = text[start : start + size]
    prefix = "..." if start > 0 else ""
    suffix = "..." if start + size < len(text) else ""
    return prefix + snippet + suffix
