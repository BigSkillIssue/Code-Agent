"""Command-line entry point for Forge."""

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from forge import __version__
from forge.config import ConfigError, ForgeConfig, find_project_root, load_config, render_effective
from forge.evals import EvalResult, EvalTask, load_tasks, results_table, run_eval
from forge.events import SessionDone
from forge.local.rich_renderer import RichRenderer
from forge.pipeline import PipelineError, report_text, resume, run_task
from forge.ports import SessionNotFoundError
from forge.wiring import close_session, default_store, open_session, show_events, use_fake_provider

EPILOG = """\
commands:
  forge "<prompt>"     work on a task
  forge config check   validate and print the effective configuration
  forge sessions       list this project's sessions
  forge resume [ID]    continue a session's plan (default: the latest)
  forge eval           run the benchmark tasks in evals/tasks (--fake: offline)
"""


def build_parser() -> argparse.ArgumentParser:
    """Create the parser for options that apply to every command."""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge coding agent.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"forge {__version__}")
    parser.add_argument("-p", "--profile", help="config profile to apply, e.g. 'ci'")
    parser.add_argument("-C", "--cwd", type=Path, help="run as if started in this folder")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="approve every tool call and use default answers"
    )
    parser.add_argument(
        "--fake",
        metavar="SCRIPT.json",
        help="replace every model with a FakeProvider replaying SCRIPT (offline runs and tests)",
    )
    return parser


def split_fake_flag(argv: list[str]) -> list[str]:
    """`--fake` may stand alone (default script) or take a `.json` path; normalise to `--fake=X`."""
    out: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--fake":
            following = argv[index + 1] if index + 1 < len(argv) else ""
            if following.endswith(".json"):
                out.append(f"--fake={following}")
                index += 2
                continue
            out.append("--fake=")
        else:
            out.append(arg)
        index += 1
    return out


def load(options: argparse.Namespace) -> tuple[Path, ForgeConfig]:
    """Find the project root and load its configuration (with --fake applied)."""
    root = find_project_root(options.cwd or Path.cwd())
    cfg = load_config(root, profile=options.profile)
    if options.fake is not None:
        use_fake_provider(cfg, Path(options.fake) if options.fake else None, root)
    return root, cfg


def cmd_config(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge config check`: validate and print the effective configuration."""
    parser = argparse.ArgumentParser(prog="forge config")
    parser.add_argument("action", choices=["check"])
    parser.parse_args(rest)
    try:
        _, cfg = load(options)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(render_effective(cfg), end="")
    return 0


def cmd_prompt(options: argparse.Namespace, prompt: str) -> int:
    """`forge "<prompt>"`: run the agent on one task and stream what it does."""
    try:
        root, cfg = load(options)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return asyncio.run(run_prompt(root, cfg, prompt, auto_approve=options.yes))


async def run_prompt(root: Path, cfg: ForgeConfig, prompt: str, *, auto_approve: bool) -> int:
    """Run one task in a fresh session; exit code 0 when the agent finished."""
    renderer = RichRenderer(auto_approve=auto_approve)
    ctx = await open_session(root, cfg, renderer)
    shower = asyncio.create_task(show_events(ctx.bus.subscribe(ctx.session.id), renderer))
    try:
        report = await run_task(prompt, ctx)
        ok = report.ok
        event = SessionDone(
            session_id=ctx.session.id, ts=time.time(), ok=ok, report=report_text(report)
        )
        await ctx.bus.publish(event)
        await shower
    finally:
        shower.cancel()
        await close_session(ctx)
    return 0 if ok else 1


def cmd_sessions(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge sessions`: list this project's sessions, newest first."""
    root, _ = load(options)
    return asyncio.run(list_sessions(root))


async def list_sessions(root: Path) -> int:
    """Print one line per session: id, date, status, goal."""
    store = default_store()
    try:
        sessions = await store.list_sessions(str(root))
    finally:
        await store.close()
    if not sessions:
        print("no sessions yet")
    for session in sessions:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.created_at))
        goal = (
            session.spec.goal
            if session.spec
            else (session.messages[0].text()[:60] if session.messages else "")
        )
        print(f"{session.id[:8]}  {when}  {session.status:<9}  {goal}")
    return 0


def cmd_resume(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge resume [ID]`: continue a session's plan at its first unfinished step."""
    parser = argparse.ArgumentParser(prog="forge resume")
    parser.add_argument("session", nargs="?", help="session id or prefix (default: the latest)")
    args = parser.parse_args(rest)
    root, cfg = load(options)
    return asyncio.run(resume_session(root, cfg, args.session, auto_approve=options.yes))


async def resume_session(
    root: Path, cfg: ForgeConfig, wanted: str | None, *, auto_approve: bool
) -> int:
    """Load a session and run the rest of its plan."""
    store = default_store()
    candidates = [s for s in await store.list_sessions(str(root), limit=100) if s.plan is not None]
    matching = [s for s in candidates if wanted is None or s.id.startswith(wanted)]
    if not matching:
        await store.close()
        print(f"error: no session with a plan matches '{wanted or 'latest'}'", file=sys.stderr)
        return 1
    renderer = RichRenderer(auto_approve=auto_approve)
    ctx = await open_session(root, cfg, renderer, store=store, session=matching[0])
    shower = asyncio.create_task(show_events(ctx.bus.subscribe(ctx.session.id), renderer))
    try:
        plan = await resume(ctx)
        ok = all(s.status in ("done", "skipped") for s in plan.steps)
        ctx.session.status = "done" if ok else "failed"
        await store.save_session(ctx.session)
        report = "all steps done" if ok else "some steps did not finish"
        await ctx.bus.publish(
            SessionDone(session_id=ctx.session.id, ts=time.time(), ok=ok, report=report)
        )
        await shower
    except (PipelineError, SessionNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ok = False
    finally:
        shower.cancel()
        await close_session(ctx)
    return 0 if ok else 1


def cmd_eval(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge eval`: run benchmark tasks and print pass/fail, cost and time."""
    parser = argparse.ArgumentParser(prog="forge eval")
    parser.add_argument("--suite", help="only tasks whose name or category starts with this")
    parser.add_argument(
        "--evals", type=Path, help="folder holding tasks/ and repos/ (default: ./evals)"
    )
    args = parser.parse_args(rest)
    root = find_project_root(options.cwd or Path.cwd())
    cfg = load_config(root, profile=options.profile)
    evals_dir = args.evals or root / "evals"
    tasks = load_tasks(evals_dir, args.suite)
    if not tasks:
        print(f"error: no eval tasks in {evals_dir / 'tasks'}", file=sys.stderr)
        return 1
    results = asyncio.run(run_evals(tasks, evals_dir, cfg, fake=options.fake is not None))
    print(results_table(results))
    return 0 if all(r.passed for r in results) else 1


async def run_evals(
    tasks: list[EvalTask], evals_dir: Path, cfg: ForgeConfig, *, fake: bool
) -> list[EvalResult]:
    """Run the tasks one after another, printing each result as it comes."""
    results = []
    for task in tasks:
        result = await run_eval(task, evals_dir, cfg, fake=fake)
        print(
            f"{'pass' if result.passed else 'FAIL'}  {task.name}  {result.note}".rstrip(),
            flush=True,
        )
        results.append(result)
    return results


COMMANDS: dict[str, Callable[[argparse.Namespace, list[str]], int]] = {
    "eval": cmd_eval,
    "config": cmd_config,
    "sessions": cmd_sessions,
    "resume": cmd_resume,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    args = split_fake_flag(list(sys.argv[1:] if argv is None else argv))
    options, rest = parser.parse_known_args(args)
    if rest and rest[0] in COMMANDS:
        return COMMANDS[rest[0]](options, rest[1:])
    unknown = [arg for arg in rest if arg.startswith("-")]
    if unknown:
        parser.error(f"unknown options: {' '.join(unknown)}")
    if rest:
        return cmd_prompt(options, " ".join(rest))
    parser.print_help()
    return 0
