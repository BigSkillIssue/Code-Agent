"""Command-line entry point for Forge."""

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from forge import __version__
from forge.agent import run_agent
from forge.config import ConfigError, ForgeConfig, find_project_root, load_config, render_effective
from forge.events import SessionDone
from forge.local.rich_renderer import RichRenderer
from forge.wiring import close_session, open_session, show_events, use_fake_provider

EPILOG = """\
commands:
  forge "<prompt>"     work on a task
  forge config check   validate and print the effective configuration
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
        result = await run_agent(ctx, prompt, max_turns=cfg.limits.max_turns_per_step)
        ok = result.stopped == "done"
        report = result.text if ok else f"{result.stopped}: {result.text}"
        await ctx.bus.publish(
            SessionDone(session_id=ctx.session.id, ts=time.time(), ok=ok, report=report)
        )
        await shower
    finally:
        await close_session(ctx)
    return 0 if ok else 1


COMMANDS: dict[str, Callable[[argparse.Namespace, list[str]], int]] = {
    "config": cmd_config,
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
