"""`forge eval`: the benchmark suite, mode comparisons, per-model reports and SWE-bench Lite."""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from forge.config import ForgeConfig, find_project_root, load_config
from forge.evals import (
    EvalResult,
    EvalTask,
    append_results,
    compare_table,
    load_tasks,
    results_row,
    results_table,
    run_eval,
    with_model,
)
from forge.swebench import load_instances, run_instance


def cmd_eval(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge eval`: run benchmark tasks and print pass/fail, cost and time."""
    parser = argparse.ArgumentParser(prog="forge eval")
    parser.add_argument("--suite", help="only tasks whose name or category starts with this")
    parser.add_argument(
        "--evals", type=Path, help="folder holding tasks/ and repos/ (default: ./evals)"
    )
    parser.add_argument(
        "--compare", help="run every task in each mode, e.g. 'solo,team'; ok if the last wins"
    )
    parser.add_argument(
        "--models", help="run the suite once per model, e.g. 'openai/gpt-5,anthropic/claude-sonnet'"
    )
    parser.add_argument(
        "--report", type=Path, help="append a results row per model to this Markdown file"
    )
    parser.add_argument(
        "--swebench", type=Path, help="run SWE-bench Lite instances from this JSONL file"
    )
    parser.add_argument("--limit", type=int, help="with --swebench: only the first N instances")
    args = parser.parse_args(rest)
    if args.swebench:
        return cmd_swebench(options, args)
    root = find_project_root(options.cwd or Path.cwd())
    cfg = load_config(root, profile=options.profile)
    evals_dir = args.evals or root / "evals"
    tasks = load_tasks(evals_dir, args.suite)
    if not tasks:
        print(f"error: no eval tasks in {evals_dir / 'tasks'}", file=sys.stderr)
        return 1
    fake = options.fake is not None
    if args.compare:
        return compare_modes(tasks, evals_dir, cfg, args.compare.split(","), fake=fake)
    models = args.models.split(",") if args.models else ["fake" if fake else "configured"]
    rows, ok = [], True
    for model in models:
        run_cfg = with_model(cfg, model) if args.models and not fake else cfg
        results = asyncio.run(run_evals(tasks, evals_dir, run_cfg, fake=fake, mode=options.mode))
        print(results_table(results))
        rows.append(results_row(model, args.suite or "all", results, time.strftime("%Y-%m-%d")))
        ok = ok and all(r.passed for r in results)
    if args.report:
        append_results(args.report, rows)
        print(f"results added to {args.report}")
    return 0 if ok else 1


def cmd_swebench(options: argparse.Namespace, args: argparse.Namespace) -> int:
    """`forge eval --swebench FILE.jsonl`: run SWE-bench Lite instances."""
    root = find_project_root(options.cwd or Path.cwd())
    cfg = load_config(root, profile=options.profile)
    instances = load_instances(args.swebench, args.limit)
    results = []
    for instance in instances:
        result = asyncio.run(run_instance(instance, cfg, fake=options.fake is not None))
        print(
            f"{'pass' if result.passed else 'FAIL'}  {result.name}  {result.note}".rstrip(),
            flush=True,
        )
        results.append(result)
    print(results_table(results))
    if args.report:
        append_results(
            args.report,
            [results_row("configured", "swe-bench-lite", results, time.strftime("%Y-%m-%d"))],
        )
    return 0 if all(r.passed for r in results) else 1


def compare_modes(
    tasks: list[EvalTask], evals_dir: Path, cfg: ForgeConfig, modes: list[str], *, fake: bool
) -> int:
    """Run the tasks once per mode; exit 0 only if the last mode passes more than the first."""
    if len(modes) < 2 or any(m not in ("solo", "subagents", "team") for m in modes):
        print("error: --compare takes two or more of solo, subagents, team", file=sys.stderr)
        return 2
    results = {m: asyncio.run(run_evals(tasks, evals_dir, cfg, fake=fake, mode=m)) for m in modes}
    print(compare_table(results))
    first, last = (sum(r.passed for r in results[m]) for m in (modes[0], modes[-1]))
    verdict = "passes more" if last > first else "does not pass more"
    print(f"{modes[-1]} {verdict} tasks than {modes[0]} ({last} vs {first})")
    return 0 if last > first else 1


async def run_evals(
    tasks: list[EvalTask], evals_dir: Path, cfg: ForgeConfig, *, fake: bool, mode: str | None
) -> list[EvalResult]:
    """Run the tasks one after another, printing each result as it comes."""
    results = []
    for task in tasks:
        result = await run_eval(task, evals_dir, cfg, fake=fake, mode=mode)
        print(
            f"{'pass' if result.passed else 'FAIL'}  {task.name}  {result.note}".rstrip(),
            flush=True,
        )
        results.append(result)
    return results
