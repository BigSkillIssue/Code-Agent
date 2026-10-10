"""`forge app new` (S64): a new full-stack product from the template."""

import argparse
import sys
from pathlib import Path

from forge.app_template import name_problem, title_of, title_problem, write_app

USAGE = "forge app new NAME [--title TITLE]"


def cmd_app(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge app ...`: make, run and check full-stack products."""
    parser = argparse.ArgumentParser(prog="forge app", usage=USAGE)
    parser.add_argument("action", choices=["new"])
    parser.add_argument("name", help="the product's name: lowercase, e.g. tally (also its folder)")
    parser.add_argument("--title", help="the name people see (default: from NAME, e.g. Tally)")
    args = parser.parse_args(rest)
    title = args.title or title_of(args.name)
    problem = name_problem(args.name) or title_problem(title)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    folder = (options.cwd or Path.cwd()) / args.name
    try:
        written = write_app(folder, args.name, title)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {title} ({len(written)} files) in {folder}")
    print(f'next: cd {folder} && forge --app "<what the product should do>"')
    print("The server's tests need PostgreSQL; see README.md.")
    return 0
