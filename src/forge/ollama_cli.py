"""`forge ollama setup` and `forge ollama status` (S57)."""

import argparse
import asyncio
import sys
from pathlib import Path

from forge.config import ForgeConfig, find_project_root, forge_home, load_config
from forge.ollama_setup import (
    api_root,
    context_warnings,
    create_variant,
    detect_hardware,
    model_capabilities,
    pull,
    recommend,
    server_status,
    write_setup,
)

NOT_RUNNING = """\
Ollama is not running at {api}.
  Install it from https://ollama.com/download (Windows, macOS, Linux), then start it:
  the desktop app starts it for you, or run `ollama serve` in a terminal."""


def cmd_ollama(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge ollama setup [--model M] [--context N] [--yes]` or `forge ollama status`."""
    parser = argparse.ArgumentParser(prog="forge ollama")
    parser.add_argument("action", choices=["setup", "status"])
    parser.add_argument("--model", help="Ollama model to use instead of the suggestion")
    parser.add_argument("--context", type=int, help="context window in tokens")
    parser.add_argument("--yes", action="store_true", help="do not ask before downloading")
    args = parser.parse_args(rest)
    cfg = load_config(find_project_root(options.cwd or Path.cwd()), profile=options.profile)
    if args.action == "status":
        return asyncio.run(status(cfg))
    return asyncio.run(setup(cfg, args.model, args.context, args.yes or options.yes))


async def status(cfg: ForgeConfig) -> int:
    """Server, installed models, and whether Forge's Ollama models have a context window."""
    api = api_root(cfg)
    found = await server_status(api)
    if found is None:
        print(NOT_RUNNING.format(api=api))
        return 1
    print(f"Ollama {found.version} at {api}")
    print("models: " + (", ".join(found.models) or "none (run `forge ollama setup`)"))
    used = sorted({m for chain in cfg.roles.values() for m in chain if m.startswith("ollama/")})
    print("Forge roles use: " + (", ".join(used) or "no Ollama model"))
    for warning in context_warnings(cfg):
        print(f"warning: {warning}")
    return 0


async def setup(cfg: ForgeConfig, model: str | None, context: int | None, yes: bool) -> int:
    """Pick, download and prepare a model, then point Forge's roles at it."""
    api = api_root(cfg)
    found = await server_status(api)
    if found is None:
        print(NOT_RUNNING.format(api=api))
        return 1
    hardware = detect_hardware()
    choice = recommend(hardware)
    print(f"hardware: {hardware.ram_gb:.0f} GB RAM, {hardware.vram_gb:.0f} GB GPU memory")
    print(f"suggestion: {choice.model} ({choice.reason})")
    model = model or choice.model
    context = context or choice.context
    if model not in found.models and not yes and not confirm(f"download {model} now?"):
        return 1
    try:
        if model not in found.models:
            await pull(api, model, lambda line: print(f"  {line}", end="\r", flush=True))
            print()
        variant = await create_variant(api, model, context)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    tools = "tools" in await model_capabilities(api, variant)
    path = forge_home() / "forge.toml"
    write_setup(path, variant, context, tools)
    print(f"created {variant} with a {context}-token context window")
    print(f"Forge's roles now use ollama/{variant} (written to {path})")
    if not tools:
        print("note: this model has no native tool calling; Forge uses prompt-based tools")
    print('try it: forge "explain this project"')
    return 0


def confirm(question: str) -> bool:
    """Ask a yes/no question on the terminal (no answer: no)."""
    if not sys.stdin.isatty():
        print(f"{question} (not asked: no terminal; add --yes)")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
