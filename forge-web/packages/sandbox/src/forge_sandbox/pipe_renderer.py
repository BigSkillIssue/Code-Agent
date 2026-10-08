"""A Forge Renderer whose questions and approvals travel to the server and wait for the answer.

Every question or approval becomes a `request` message with an id; the answer arrives later as
an `answer` message with the same id. Malformed answers count as a refusal (approvals) or as
"dismissed" (questions), never as a yes.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from forge.events import Event
from forge.plan import Question
from forge.ports import Answer, Approval
from forge.providers.base import ToolCall
from pydantic import ValidationError

Send = Callable[[dict[str, Any]], Awaitable[None]]


class PipeRenderer:
    """Forwards ask/approve to the daemon; `resolve` completes them."""

    def __init__(self, send: Send, *, auto_plans: bool = False) -> None:
        self.send = send
        self.auto_plans = auto_plans  # approve plans without asking ("auto" mode)
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def show(self, event: Event) -> None:
        """Events reach the server through the event bus, not through here."""

    async def ask(self, questions: list[Question]) -> list[Answer]:
        """Ask the user; an empty list means they dismissed the questions."""
        payload = {"questions": [q.model_dump(mode="json") for q in questions]}
        reply = await self._request("question", payload)
        if reply.get("dismissed"):
            return []
        try:
            return [Answer.model_validate(a) for a in reply.get("answers", [])]
        except (ValidationError, TypeError):
            return []

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Ask the user whether `call` may run."""
        if self.auto_plans and call.name == "submit_plan":
            return Approval(allow=True)
        payload = {"call": call.model_dump(mode="json"), "reason": reason}
        reply = await self._request("approval", payload)
        try:
            return Approval.model_validate(reply)
        except ValidationError:
            return Approval(allow=False, feedback="the answer could not be read")

    async def _request(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"r{secrets.token_hex(6)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[request_id] = future
        try:
            await self.send({"type": "request", "id": request_id, "kind": kind, "payload": payload})
            return await future
        finally:
            self.waiting.pop(request_id, None)

    def resolve(self, request_id: str, answer: Any) -> bool:
        """Complete a waiting request; False if none with that id is waiting."""
        future = self.waiting.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(answer if isinstance(answer, dict) else {})
        return True
