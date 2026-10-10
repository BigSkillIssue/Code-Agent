"""`forge app new|dev|check` (S64, S65): make, run and check full-stack products."""

import argparse
import asyncio
import sys
from pathlib import Path

from forge.app_checks import check_app, report_text, save_report
from forge.app_dev import run_dev
from forge.app_template import name_problem, title_of, title_problem, write_app
from forge.local.local_executor import LocalExecutor
from forge.ports import SandboxPolicy

USAGE = """\
forge app new NAME [--title TITLE]             a new product in ./NAME
forge app dev [--database-url URL]             run it (on a throwaway PostgreSQL by default)
forge app check [--database-url URL] [--json]  the fixed checks; .forge/out/app/checks.json"""
# The user runs these commands themselves, like a shell command, so they run unsandboxed.
LOCAL = SandboxPolicy(mode="full-access", network=True)


def cmd_app(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge app ...`: make, run and check full-stack products."""
    parser = argparse.ArgumentParser(prog="forge app", usage=USAGE)
    parser.add_argument("action", choices=["new", "dev", "check"])
    parser.add_argument("name", nargs="?", help="new: the product's name, lowercase (its folder)")
    parser.add_argument("--title", help="new: the name people see (default: from NAME)")
    parser.add_argument("--database-url", help="dev, check: a throwaway database to use")
    parser.add_argument("--json", action="store_true", help="check: print the report as JSON")
    args = parser.parse_args(rest)
    cwd = (options.cwd or Path.cwd()).resolve()
    if args.action == "new":
        return new(cwd, args.name, args.title)
    if args.action == "dev":
        return asyncio.run(dev(cwd, args.database_url))
    return asyncio.run(check(cwd, args.database_url, args.json))


def new(cwd: Path, name: str | None, title: str | None) -> int:
    """`forge app new NAME`."""
    if not name:
        print(f"usage: {USAGE.splitlines()[0]}", file=sys.stderr)
        return 2
    title = title or title_of(name)
    problem = name_problem(name) or title_problem(title)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    folder = cwd / name
    try:
        written = write_app(folder, name, title)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {title} ({len(written)} files) in {folder}")
    print(f'next: cd {folder} && forge --app "<what the product should do>"')
    print("Run it with `forge app dev`; the server's tests need PostgreSQL (see README.md).")
    return 0


async def dev(root: Path, database_url: str | None) -> int:
    """`forge app dev`: run until ctrl+c."""
    executor = LocalExecutor(root)
    try:
        return await run_dev(root, executor, LOCAL, print, asyncio.Event(), database_url)
    finally:
        await executor.close()


async def check(root: Path, database_url: str | None, as_json: bool) -> int:
    """`forge app check`: every gate, the report saved and printed."""
    executor = LocalExecutor(root)
    show = None if as_json else (lambda gate: print(f"{gate.status:7} {gate.gate}", flush=True))
    try:
        report = await check_app(root, executor, LOCAL, database_url, on_gate=show)
    finally:
        await executor.close()
    path = save_report(root, report)
    print(report.model_dump_json(indent=2) if as_json else f"\n{report_text(report)}\n({path})")
    return 0 if report.ok else 1
