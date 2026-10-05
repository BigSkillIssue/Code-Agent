"""Gather what the refiner needs to know about the project (no model calls)."""

from forge.ctx import Ctx
from forge.memory import load_memory, render_memory
from forge.runtime.ignore import project_files
from forge.runtime.proc import run_argv, which
from forge.runtime.tree import build_tree, render_tree

TREE_DEPTH = 3
MAX_STATUS_LINES = 50


async def gather(ctx: Ctx) -> str:
    """File tree (depth 3), git status, memory files and the last session's summary."""
    files = await project_files(ctx.root)
    tree = render_tree(".", build_tree(ctx.root, files, TREE_DEPTH), TREE_DEPTH)
    parts = [f"File tree:\n{tree}", f"Git status:\n{await git_status(ctx)}"]
    parts.append(
        f"Project instructions:\n{render_memory(load_memory(ctx.root, ctx.cwd), ctx.root)}"
    )
    summary = await last_summary(ctx)
    if summary:
        parts.append(f"Summary of the previous session:\n{summary}")
    return "\n\n".join(parts)


async def git_status(ctx: Ctx) -> str:
    """`git status --short`, or a note when this is not a git repository."""
    if which("git") is None:
        return "(git not installed)"
    result = await run_argv(["git", "status", "--short", "--branch"], ctx.root)
    if result.code != 0:
        return "(not a git repository)"
    lines = result.stdout.splitlines()
    extra = len(lines) - MAX_STATUS_LINES
    shown = lines[:MAX_STATUS_LINES] + ([f"... {extra} more"] if extra > 0 else [])
    return "\n".join(shown) or "(clean)"


async def last_summary(ctx: Ctx) -> str:
    """The summary of the most recent earlier session of this project, if any."""
    for session in await ctx.store.list_sessions(str(ctx.root), limit=5):
        if session.id != ctx.session.id and session.summary:
            return session.summary
    return ""
