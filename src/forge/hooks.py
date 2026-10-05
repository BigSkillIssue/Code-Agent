"""Hooks: user commands that run on events such as `pre_tool` (stub until S37: none run)."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from forge.config import ForgeConfig, HookEventName

HookEvent = HookEventName


class HookOutcome(BaseModel):
    """What the hooks for one event decided."""

    block: bool = False
    message: str = ""  # shown to the model when blocking


class Hooks:
    """Runs the configured hooks of a session."""

    def __init__(self, cfg: ForgeConfig) -> None:
        self.cfg = cfg

    async def run(
        self, event: HookEvent, payload: Mapping[str, Any], ctx: Any = None
    ) -> HookOutcome:
        """Run every hook registered for `event`; no hooks exist yet."""
        return HookOutcome()
