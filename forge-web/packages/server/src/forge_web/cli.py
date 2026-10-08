"""The `forge-web` command: run the server and manage it."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from forge_web import __version__
from forge_web.settings import SettingsError, WebSettings, load_settings


def build_parser() -> argparse.ArgumentParser:
    """The argument parser with every subcommand."""
    parser = argparse.ArgumentParser(prog="forge-web", description="Forge Web server.")
    parser.add_argument("--version", action="version", version=f"forge-web {__version__}")
    parser.add_argument("--config", type=Path, help="settings file (default: in the data folder)")
    parser.add_argument("--data-dir", type=Path, help="data folder (database, keys, logs)")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="run the web server")
    serve.add_argument("--host", help="address to listen on (default from settings)")
    serve.add_argument("--port", type=int, help="port to listen on (default from settings)")
    serve.add_argument(
        "--dev", action="store_true", help="development mode: local sandboxes, a login link"
    )
    serve.add_argument(
        "--fake", nargs="?", const="", metavar="SCRIPT", help="every chat uses the fake model"
    )
    from forge_web.dev_chat import add_arguments

    add_arguments(commands.add_parser("dev-chat", help="chat with Forge in the terminal (local)"))
    from forge_web import sandbox_cli

    sandbox_cli.add_arguments(commands.add_parser("sandbox", help="manage the sandbox image"))
    from forge_web import user_cli

    user_cli.add_arguments(commands.add_parser("user", help="manage accounts"))
    commands.add_parser("doctor", help="check what the server needs and how to fix it")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        settings = settings_from(args)
    except SettingsError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if args.command == "serve":
        return serve(settings)
    if args.command == "user":
        from forge_web import user_cli

        return user_cli.run(settings, args)
    if args.command == "sandbox":
        from forge_web import sandbox_cli

        return sandbox_cli.run(settings, args)
    if args.command == "doctor":
        from forge_web.doctor import doctor

        return doctor(settings)
    if args.command == "dev-chat":
        from forge_web.dev_chat import dev_chat

        return asyncio.run(dev_chat(settings, args))
    return 0


def settings_from(args: argparse.Namespace) -> WebSettings:
    """Settings from the file and environment, with the command-line options on top."""
    overrides: dict[str, object] = {}
    if args.data_dir is not None:
        overrides["data_dir"] = str(args.data_dir)
    for key in ("host", "port"):
        if getattr(args, key, None) is not None:
            overrides[f"server.{key}"] = getattr(args, key)
    if getattr(args, "dev", False):
        overrides["dev.enabled"] = True
        overrides["sandbox.isolation"] = "local"
    if getattr(args, "fake", None) is not None:
        overrides["dev.fake"] = True
        overrides["dev.fake_script"] = args.fake
    return load_settings(args.config, overrides=overrides)


def serve(settings: WebSettings) -> int:
    """Serve until interrupted (one process: run state lives in memory)."""
    import uvicorn

    from forge_web.app import create_app

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    config = uvicorn.Config(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        proxy_headers=True,
        log_level="info",
        ws="websockets-sansio",
    )
    # asyncio.run keeps the default loop (Proactor on Windows), which subprocesses need.
    asyncio.run(uvicorn.Server(config).serve())
    return 0
