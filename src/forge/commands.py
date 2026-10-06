"""Slash commands typed by the user: built-ins, and custom ones from `.forge/commands/*.md`.

A custom command file holds a prompt template; `$ARGUMENTS` is replaced by what follows
the command name, and the result runs as a task (`/review src/`).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.config import forge_home
from forge.ctx import Ctx
from forge.mcp_cli import mcp_command
from forge.plan import checklist
from forge.runtime.checkpoint import CheckpointError, restore
from forge.runtime.files import (
    FileChange,
    apply_changes,
    display_path,
    journal_dir,
    undo_last_change,
)
from forge.runtime.ignore import project_files
from forge.tasks_view import stop_task, task_detail, tasks_report

HELP = {
    "/plan": "show the plan with step statuses",
    "/go": "continue the plan from its first unfinished step",
    "/compact [hard]": "summarize the context now (hard: reset it)",
    "/context": "tokens used by the last request, by category",
    "/undo": "roll back the last step or file change",
    "/mode [MODE]": "show or set the sandbox mode or approval policy",
    "/tasks [stop] [ID]": "background jobs, agents and monitors; show or stop one",
    "/mcp [add|remove|reconnect]": "MCP servers: status, or change them without a restart",
    "/jobs": "background jobs and their status",
    "/agents": "the session's agents with status and usage",
    "/init": "write a FORGE.md with this project's commands and layout",
    "/help": "this list",
}
SANDBOX_MODES = ("read-only", "workspace-write", "full-access")
APPROVAL_POLICIES = ("on-request", "always", "never")
COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class SlashResult:
    """What a command produced: text to show, a prompt to run as a task, or 'continue the plan'."""

    text: str = ""
    prompt: str | None = None
    resume: bool = False


async def run_command(ctx: Ctx, text: str) -> str:
    """Run one slash command and return what to show the user."""
    result = await handle_command(ctx, text)
    if result.prompt is not None:
        return f"running: {result.prompt}"
    return result.text


async def handle_command(ctx: Ctx, text: str) -> SlashResult:
    """Run a built-in command, or expand a custom one into a task prompt."""
    name, _, args = text.strip().partition(" ")
    args = args.strip()
    builtin = await run_builtin(ctx, name, args)
    if builtin is not None:
        return builtin
    custom = custom_commands(ctx.root).get(name.lstrip("/"))
    if custom is not None:
        return SlashResult(prompt=expand(custom.template, args))
    return SlashResult(text=f"unknown command {name}; /help lists the commands")


async def run_builtin(ctx: Ctx, name: str, args: str) -> SlashResult | None:
    """A built-in command's result, or None when `name` is not one."""
    texts: dict[str, Any] = {
        "/undo": lambda: undo(ctx),
        "/compact": lambda: request_compaction(ctx, hard=args == "hard"),
        "/context": lambda: context_report(ctx),
        "/plan": lambda: checklist(ctx.session.plan) if ctx.session.plan else "no plan yet",
        "/mode": lambda: set_mode(ctx, args),
        "/tasks": lambda: tasks_command(ctx, args),
        "/mcp": lambda: mcp_command(ctx, args),
        "/jobs": lambda: jobs_report(ctx),
        "/agents": lambda: ctx.state.team.overview(ctx) if ctx.state.team else "no agents",
        "/init": lambda: init_forge_md(ctx),
        "/help": lambda: help_text(ctx.root),
    }
    if name == "/go":
        return go(ctx)
    if name not in texts:
        return None
    value = texts[name]()
    if not isinstance(value, str):
        value = await value
    return SlashResult(text=value)


async def tasks_command(ctx: Ctx, args: str) -> str:
    """/tasks, /tasks <id>, /tasks stop <id>."""
    words = args.split()
    if not words:
        return await tasks_report(ctx)
    if words[0] == "stop" and len(words) == 2:
        return await stop_task(ctx, words[1])
    return await task_detail(ctx, words[0])


def go(ctx: Ctx) -> SlashResult:
    """/go: continue the plan, if one has unfinished steps."""
    plan = ctx.session.plan
    if plan is None or all(s.status in ("done", "skipped") for s in plan.steps):
        return SlashResult(text="no plan to continue")
    return SlashResult(text="continuing the plan", resume=True)


def help_text(root: Path) -> str:
    """Built-in and custom commands."""
    lines = [f"{command:<18}{text}" for command, text in HELP.items()]
    for name, custom in sorted(custom_commands(root).items()):
        lines.append(f"{'/' + name:<18}{custom.description}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- custom commands


@dataclass
class CustomCommand:
    """A prompt template from `<commands dir>/<name>.md`."""

    name: str
    description: str
    template: str


def custom_commands(root: Path) -> dict[str, CustomCommand]:
    """User commands, then project commands (the project wins on a name clash)."""
    found: dict[str, CustomCommand] = {}
    for folder in (forge_home() / "commands", root / ".forge" / "commands"):
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            if COMMAND_NAME.match(path.stem):
                found[path.stem] = read_command(path)
    return found


def read_command(path: Path) -> CustomCommand:
    """Optional front matter (`description:`), then the template."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    description = ""
    if text.startswith("---\n") and "\n---" in text[3:]:
        head, _, rest = text[4:].partition("\n---")
        text = rest.partition("\n")[2]
        for line in head.splitlines():
            key, colon, value = line.partition(":")
            if colon and key.strip() == "description":
                description = value.strip()
    template = text.strip()
    return CustomCommand(path.stem, description or template.splitlines()[0][:60], template)


def expand(template: str, arguments: str) -> str:
    """Put the arguments where the template says $ARGUMENTS (or after it, if it does not)."""
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", arguments)
    return f"{template}\n\n{arguments}".strip()


# ----------------------------------------------------------------------------- /init


async def init_forge_md(ctx: Ctx) -> str:
    """/init: write FORGE.md with the project's build and test commands and its layout."""
    target = ctx.root / "FORGE.md"
    if target.exists():
        return "FORGE.md already exists; edit it, or delete it and run /init again"
    files = await project_files(ctx.root)
    text = forge_md(ctx.root, [f.relative_to(ctx.root).as_posix() for f in files])
    await apply_changes(ctx, [FileChange(target, text.encode("utf-8"))])
    lines = len(text.splitlines())
    return f"wrote FORGE.md ({lines} lines); check the commands and add your conventions"


def forge_md(root: Path, files: list[str]) -> str:
    """The FORGE.md text for a project."""
    commands = detect_commands(root, files) or [
        "- (no build or test command found; add yours here)"
    ]
    lines = [
        "# FORGE.md",
        "",
        "Notes for Forge and other coding agents working in this project.",
        "",
        "## Commands",
        "",
        *commands,
        "",
        "## Layout",
        "",
        *layout(files),
        "",
        "## Conventions",
        "",
        "- (add coding conventions here)",
        "",
    ]
    return "\n".join(lines)


def detect_commands(root: Path, files: list[str]) -> list[str]:
    """Build, test and lint commands from the project's tool files."""
    found: list[str] = []
    names = set(files)
    python = "uv run " if "uv.lock" in names else ""
    if "pyproject.toml" in names or any(is_python_test(f) for f in files):
        found.append(f"- Test: `{python or 'python -m '}pytest -q`")
        if "ruff" in read(root / "pyproject.toml"):
            found.append(f"- Lint: `{python}ruff check .`")
    if "package.json" in names:
        scripts = json.loads(read(root / "package.json") or "{}").get("scripts", {})
        found += [
            f"- {name.title()}: `npm run {name}`"
            for name in ("build", "test", "lint")
            if name in scripts
        ]
    if "Cargo.toml" in names:
        found += ["- Build: `cargo build`", "- Test: `cargo test`"]
    if "go.mod" in names:
        found += ["- Build: `go build ./...`", "- Test: `go test ./...`"]
    if "Makefile" in names:
        targets = re.findall(r"^([a-zA-Z][\w-]*):", read(root / "Makefile"), re.MULTILINE)
        found += [f"- {t.title()}: `make {t}`" for t in ("build", "test", "lint") if t in targets]
    return found


def is_python_test(path: str) -> bool:
    """test_*.py or *_test.py."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def layout(files: list[str], limit: int = 30) -> list[str]:
    """Top-level files and folders (with file counts)."""
    folders: dict[str, int] = {}
    top: list[str] = []
    for path in files:
        head, sep, _ = path.partition("/")
        if sep:
            folders[head] = folders.get(head, 0) + 1
        else:
            top.append(path)
    lines = [f"- `{name}/` ({count} files)" for name, count in sorted(folders.items())]
    lines += [f"- `{name}`" for name in sorted(top) if name != "FORGE.md"]
    return lines[:limit]


def read(path: Path) -> str:
    """A small text file's content, or '' when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


async def undo(ctx: Ctx) -> str:
    """Roll back the most recent step (its snapshot), or else the last file change."""
    plan = ctx.session.plan
    checkpoints = ctx.state.checkpoints
    if plan is not None and checkpoints:
        order = [s.id for s in plan.steps]
        step_id = max(checkpoints, key=lambda sid: order.index(sid) if sid in order else -1)
        try:
            changed = await restore(ctx.root, checkpoints.pop(step_id))
        except CheckpointError as err:
            return f"undo failed: {err}"
        step = plan.step(step_id)
        if step is not None:
            step.status, step.attempts, step.notes = "todo", 0, ""
            await ctx.store.save_session(ctx.session)
        return f"undid step {step_id}: {len(changed)} files restored"
    restored = undo_last_change(journal_dir(ctx))
    if not restored:
        return "nothing to undo"
    return "undid the last change: " + ", ".join(display_path(ctx.root, p) for p in restored)


def request_compaction(ctx: Ctx, hard: bool) -> str:
    """Compact on the agent's next turn: level 2, or level 3 with `/compact hard`."""
    ctx.state.compact_request = 3 if hard else 2
    return f"the context will be {'reset' if hard else 'summarized'} before the next model call"


def context_report(ctx: Ctx) -> str:
    """Tokens of the latest request by category, against the budget."""
    usage = dict(ctx.state.context_usage)
    if not usage:
        return "no model request yet"
    total, budget = usage.pop("total"), usage.pop("budget")
    share = f"{total / budget:.0%}" if budget else "?"
    lines = [f"context: {total:,} of {budget:,} tokens ({share})"]
    lines += [f"  {name:<13}{tokens:>9,}" for name, tokens in usage.items()]
    limits = ctx.cfg.limits
    lines.append(f"summary at {limits.compact_at:.0%}, reset at {limits.reset_at:.0%}")
    return "\n".join(lines)


def set_mode(ctx: Ctx, mode: str) -> str:
    """Show or change the sandbox mode or the approval policy for this session."""
    cfg = ctx.cfg
    if mode in SANDBOX_MODES:
        cfg.sandbox.mode = mode  # type: ignore[assignment]
    elif mode in APPROVAL_POLICIES:
        cfg.approval.policy = mode  # type: ignore[assignment]
    elif mode:
        return f"unknown mode {mode}; use one of {', '.join(SANDBOX_MODES + APPROVAL_POLICIES)}"
    return f"sandbox: {cfg.sandbox.mode}, approval: {cfg.approval.policy}"


def jobs_report(ctx: Ctx) -> str:
    """One line per background job of this session's executor."""
    jobs: dict[str, Any] = getattr(ctx.executor, "jobs", {})
    if not jobs:
        return "no background jobs"
    lines = []
    for job in jobs.values():
        state = "running" if job.ended is None else f"exit {job.exit_code}"
        lines.append(f"{job.id}  {state:<9} {job.elapsed():.0f}s  pid {job.proc.pid}")
    return "\n".join(lines)
