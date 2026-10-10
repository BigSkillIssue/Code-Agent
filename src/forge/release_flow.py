"""Checkpoints in the pipeline (S67a): independent checks of the request before planning, of
the plan before building and of the product at the end, where the user approves it or not.

Apple apps (S60) are the first kind of checkpoint and full-stack products (S67b) the second.
Before planning a checkpoint may work on the refined request (the product's blueprint, S68).
The pipeline runs whichever checkpoints a task has, in order; each keeps its own rules for what
blocks, what goes back to the agent and what the user decides.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from forge.ctx import Ctx
from forge.plan import Plan, TaskSpec

Replan = Callable[[TaskSpec], Awaitable[Plan]]


class CheckpointStopped(Exception):
    """A checkpoint stops the run: the user's choice, or the default without one."""


@dataclass
class Outcome:
    """What a checkpoint says about the finished product: ready or not, and why."""

    ready: bool
    summary: str


class Checkpoint(Protocol):
    """One kind of independent check, at three points of the pipeline."""

    name: str

    async def check_request(self, ctx: Ctx, prompt: str) -> None:
        """Review the request before anything is planned; raise CheckpointStopped to stop."""

    async def prepare_plan(self, ctx: Ctx, spec: TaskSpec) -> TaskSpec:
        """Work on the refined request before it is planned (e.g. a blueprint); return it."""

    async def check_plan(self, ctx: Ctx, plan: Plan, replan: Replan) -> Plan:
        """Review the plan before it is built; return it, or a plan made again."""

    async def finish(self, ctx: Ctx) -> Outcome:
        """Check the finished product and ask the user to approve it."""

    def report_fields(self, outcome: Outcome) -> dict[str, Any]:
        """The fields of the task's Report this checkpoint's outcome sets."""


async def check_request(checkpoints: list[Checkpoint], ctx: Ctx, prompt: str) -> None:
    """Every checkpoint's review of the request, in order."""
    for checkpoint in checkpoints:
        await checkpoint.check_request(ctx, prompt)


async def prepare_plan(checkpoints: list[Checkpoint], ctx: Ctx, spec: TaskSpec) -> TaskSpec:
    """Every checkpoint's work on the refined request before planning, in order."""
    for checkpoint in checkpoints:
        spec = await checkpoint.prepare_plan(ctx, spec)
    return spec


async def check_plan(checkpoints: list[Checkpoint], ctx: Ctx, plan: Plan, replan: Replan) -> Plan:
    """Every checkpoint's review of the plan; each sees the plan the one before returned."""
    for checkpoint in checkpoints:
        plan = await checkpoint.check_plan(ctx, plan, replan)
    return plan


async def finish(checkpoints: list[Checkpoint], ctx: Ctx) -> dict[str, Any]:
    """Every checkpoint's check of the product; the Report fields their outcomes set."""
    fields: dict[str, Any] = {}
    for checkpoint in checkpoints:
        fields |= checkpoint.report_fields(await checkpoint.finish(ctx))
    return fields
