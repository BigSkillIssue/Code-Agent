"""`forge-web sandbox build`: build the sandbox image from a source checkout."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from forge_web.settings import WebSettings

DOCKERFILE = Path("forge-web") / "docker" / "sandbox.Dockerfile"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The sandbox options."""
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="build the sandbox image (needs a source checkout)")
    build.add_argument("--tag", help="image name (default: sandbox.image from the settings)")
    build.add_argument(
        "--docker-arg", action="append", default=[], help="extra argument for docker build"
    )


def source_root(start: Path | None = None) -> Path | None:
    """The repository root above this file, if Forge Web runs from a checkout."""
    for folder in (start or Path(__file__).resolve()).parents:
        if (folder / DOCKERFILE).is_file() and (folder / "src" / "forge").is_dir():
            return folder
    return None


def build_command(docker: str, root: Path, tag: str, extra: list[str]) -> list[str]:
    """The docker build command line."""
    return [docker, "build", "-f", str(root / DOCKERFILE), "-t", tag, *extra, str(root)]


def run(settings: WebSettings, args: argparse.Namespace) -> int:
    """Build the image."""
    root = source_root()
    if root is None:
        print(
            "error: no source checkout found; clone the repository and run this command there",
            file=sys.stderr,
        )
        return 1
    docker = shutil.which(settings.sandbox.docker) or settings.sandbox.docker
    command = build_command(docker, root, args.tag or settings.sandbox.image, args.docker_arg)
    print("$", " ".join(command), file=sys.stderr)
    return subprocess.run(command, check=False).returncode
