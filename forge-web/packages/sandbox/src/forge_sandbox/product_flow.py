"""How Forge Web runs a task (W55): the same pipeline as Forge's `run_task`, built from Forge's
public functions, but the user steers less and the agents decide more.

- Every task is planned (no shortcut for small ones) and the refiner's questions go to the user.
- A product decision (a website, a full product with apps, or none) is the agent's: the project
  becomes a product, or gets more apps, without the user choosing it; the hosting review (Forge's
  AppCheckpoint, with its architect) and Apple's review run when the product calls for them.
- A plan whose steps span several areas runs as a team with one agent per area.
- After the coders, the required agents run (the UI agent for products with an interface, then
  the tester and the security agent); what they find goes back to the coder, a few rounds at most.
"""

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

from forge import release_flow
from forge.agent import over_budget, run_agent
from forge.agent_files import use_agent_file
from forge.app_flow import AppCheckpoint
from forge.app_manifest import load_manifest
from forge.apple_flow import AppleCheckpoint
from forge.conversation import record_turn
from forge.ctx import Ctx
from forge.modelcall import complete
from forge.pipeline import (
    PipelineError,
    PlanRejected,
    Report,
    budget_report,
    clarify,
    execute,
    final_review,
    make_plan,
    mode_for_plan,
    refine,
    stopped_report,
)
from forge.plan import Plan, TaskSpec
from forge.providers.base import text_message
from forge.runtime.attachments import Attachment, AttachmentError, check_attachments
from forge.runtime.errors import ToolError
from forge.runtime.ledger import ReadLedger
from forge.structured import StructuredError, parse_as
from pydantic import BaseModel, Field

from forge_sandbox import prompts
from forge_sandbox.methods import AgentSpec
from forge_sandbox.products.place import add, create
from forge_sandbox.products.render import ProductError

DECIDER = "product"  # the role of the product decision (the planner's models, W53)
FIX_ROUNDS = 3  # how often a required agent's findings go back to the coder
REVIEW_TURNS = 40
CLEAN = "no findings"
GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


class ProductDecision(BaseModel):
    """What the task makes of the project."""

    product: Literal["none", "site", "fullstack"] = "none"
    name: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=60)
    clients: list[Literal["apple", "android", "windows"]] = []
    payments: bool = False
    digital_goods: bool = False
    reason: str = ""


async def decide(ctx: Ctx, spec: TaskSpec) -> ProductDecision:
    """The product decision for a task; none when the model's answer cannot be read."""
    manifest = load_manifest(ctx.root / "forge.app.toml")
    present = "none" if isinstance(manifest, list) else manifest.model_dump_json()
    system = prompts.PRODUCT_DECISION
    task = prompts.PRODUCT_DECISION_TASK.format(
        spec=spec.model_dump_json(indent=2), present=present
    )
    reply, _ = await complete(ctx, DECIDER, system, [text_message("user", task)])
    try:
        return parse_as(reply.text(), ProductDecision)
    except StructuredError:
        return ProductDecision(reason="the product decision could not be read")


def slug(text: str) -> str:
    """A product name from the model's suggestion: lowercase letters, digits and dashes."""
    spelled = "".join(GERMAN.get(c, c) for c in text.lower())
    found = re.sub(r"[^a-z0-9]+", "-", spelled).strip("-")[:40].rstrip("-")
    return found if found[:1].isalpha() else f"app-{found}".strip("-")[:40]


def place(ctx: Ctx, decision: ProductDecision, apps_domain: str) -> str:
    """Make the project the product it should be; a note on what was done ("" for nothing)."""
    root = ctx.root
    manifest = load_manifest(root / "forge.app.toml")
    features = [*decision.clients, *(["payments"] if decision.payments else []),
                *(["digital_goods"] if decision.digital_goods else [])]  # fmt: skip
    try:
        if isinstance(manifest, list):
            if decision.product == "none":
                return ""
            name = slug(decision.name or decision.title or "my-product")
            own = features if decision.product == "fullstack" else []
            done = create(root, decision.product, name, decision.title or None, own, apps_domain)
        else:
            have = set(manifest.clients) | (
                {"payments"} if manifest.payments.kind == "relay" else set()
            )
            more = [f for f in features if f not in have]
            if not more:
                return ""
            done = add(root, decision.title or None, more, apps_domain)
    except ProductError as err:
        return f"The product could not be placed: {err}"
    merged = ", ".join(sorted(done.to_merge))
    return prompts.PLACED.format(written=len(done.written), merge=merged or "nothing")


def checkpoints_for_product(ctx: Ctx, apple: bool) -> list[release_flow.Checkpoint]:
    """The reviews the product needs: hosting's for any product, Apple's for an Apple client."""
    manifest = load_manifest(ctx.root / "forge.app.toml")
    if isinstance(manifest, list):
        return []
    found: list[release_flow.Checkpoint] = [AppCheckpoint()]
    if apple and "apple" in manifest.clients:
        found.insert(0, AppleCheckpoint())
    return found


def required_agents(ctx: Ctx, agents: Sequence[AgentSpec]) -> list[AgentSpec]:
    """The required agents that apply to this project now, UI first."""
    manifest = load_manifest(ctx.root / "forge.app.toml")
    product = not isinstance(manifest, list)
    applies = {"always": True, "product": product, "ui": product}
    found = [a for a in agents if a.policy == "required" and applies[a.applies_to]]
    return sorted(found, key=lambda a: a.applies_to != "ui")


async def run_as(ctx: Ctx, name: str, task: str) -> str:
    """One run of an admin's agent (a Forge agent file) on the task; its final answer."""
    try:
        await use_agent_file(ctx, name)
    except ToolError as err:
        return f"{name} could not start: {err}"
    agent = replace(ctx, agent_id=name, role=name, ledger=ReadLedger())
    result = await run_agent(agent, task, role=name, max_turns=REVIEW_TURNS)
    return result.text


async def required_pass(ctx: Ctx, spec: TaskSpec, agents: Sequence[AgentSpec]) -> list[str]:
    """Run the required agents; their findings go back to the coder. Notes for the report."""
    notes: list[str] = []
    for agent in required_agents(ctx, agents):
        if over_budget(ctx):
            break
        if agent.applies_to == "ui":  # the UI agent builds: one pass
            await run_as(ctx, agent.name, prompts.UI_TASK.format(goal=spec.goal))
            continue
        for round_ in range(FIX_ROUNDS):
            found = await run_as(ctx, agent.name, prompts.REVIEW_TASK.format(goal=spec.goal))
            if CLEAN in found.lower() or over_budget(ctx):
                break
            if round_ == FIX_ROUNDS - 1:
                notes.append(f"{agent.name} still reports: {found[:1500]}")
                break
            await run_agent(ctx, prompts.FIX_TASK.format(agent=agent.name, findings=found),
                            max_turns=ctx.cfg.limits.max_turns_per_step)  # fmt: skip
    return notes


async def run_product_planned(spec: TaskSpec, ctx: Ctx, checkpoints: list[release_flow.Checkpoint],
                              agents: Sequence[AgentSpec]) -> Report:  # fmt: skip
    """Forge's run_planned with the required agents between the coders and the final review."""
    spec = await release_flow.prepare_plan(checkpoints, ctx, spec)
    plan = await make_plan(spec, ctx)

    async def replan(revised: TaskSpec) -> Plan:
        return await make_plan(revised, ctx)

    plan = await release_flow.check_plan(checkpoints, ctx, plan, replan)
    ctx.state.mode = mode_for_plan(plan, ctx.state.mode, None)  # several areas: a team
    plan = await execute(plan, ctx)
    if over_budget(ctx):
        return await budget_report(ctx)
    notes = await required_pass(ctx, spec, agents)
    fields = await release_flow.finish(checkpoints, ctx)
    report = await final_review(plan, ctx)
    if notes:
        report = report.model_copy(update={"manual_checks": [*report.manual_checks, *notes]})
    return report.model_copy(update=fields) if fields else report


async def run_product_task(
    prompt: str,
    ctx: Ctx,
    attachments: Sequence[Attachment] = (),
    *,
    agents: Sequence[AgentSpec] = (),
    apps_domain: str = "",
    apple: bool = False,
) -> Report:
    """Forge Web's task: always planned, the product decided by the agent (see the module)."""
    ctx.session.status = "active"
    submitted = await ctx.hooks.run("prompt_submit", {"prompt": prompt}, ctx)
    if submitted.block:
        return stopped_report(ctx, f"A prompt_submit hook stopped this task: {submitted.message}")
    try:
        ctx.state.attachments = await check_attachments(ctx.root, attachments)
        spec = await refine(prompt, ctx)
        ctx.session.spec = spec
        spec = await clarify(spec, ctx)
        decision = await decide(ctx, spec)
        note = place(ctx, decision, apps_domain)
        if note:
            spec = spec.model_copy(update={"context": f"{spec.context}\n\n{note}".strip()})
        if decision.product != "none" or (ctx.root / "forge.app.toml").exists():
            rules = prompts.PRODUCT_RULES.format(domain=apps_domain or "Forge's servers")
            spec = spec.model_copy(update={"constraints": [*spec.constraints, rules]})
        ctx.state.mode = "team" if spec.size == "large" else "subagents"
        checkpoints = checkpoints_for_product(ctx, apple)
        await release_flow.check_request(checkpoints, ctx, prompt)
        report = await run_product_planned(spec, ctx, checkpoints, agents)
    except PlanRejected:
        report = stopped_report(ctx, "You rejected the plan, so nothing was changed.")
    except (PipelineError, release_flow.CheckpointStopped, AttachmentError) as err:
        report = stopped_report(ctx, str(err))
    finally:
        ctx.state.attachments = []
    await record_turn(ctx, prompt, report.ok, report.summary, report.files_changed)
    ctx.session.status = "done" if report.ok else "failed"
    ctx.session.summary = report.summary
    await ctx.store.save_session(ctx.session)
    await ctx.hooks.run("stop", {"ok": report.ok, "summary": report.summary}, ctx)
    return report


def is_product(root: Path) -> bool:
    """Whether the project is a product (it has a valid forge.app.toml)."""
    return not isinstance(load_manifest(root / "forge.app.toml"), list)
