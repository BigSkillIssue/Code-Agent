"""Embed Forge in your own program.

    uv run python examples/embed.py "Fix the failing test"    # real models from your config
    uv run python examples/embed.py --fake                    # offline, scripted model

The example runs in a throwaway copy of examples/buggy, so it never touches your files.
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from forge import Forge
from forge.config import load_config
from forge.events import ModelDelta, SessionDone, ToolStarted
from forge.local.memory_store import MemoryStore
from forge.providers.fake import FakeProvider
from forge.wiring import install_fake

BUGGY = Path(__file__).parent / "buggy"
FIX_SCRIPT = Path(__file__).parents[1] / "tests" / "fixtures" / "fake" / "fix_buggy.json"


async def main(prompt: str, fake: bool) -> int:
    """Fix the buggy example with Forge, printing what happens as it streams."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "buggy"
        shutil.copytree(BUGGY, root)
        for git_args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
            identity = ["-c", "user.name=Example", "-c", "user.email=example@localhost"]
            subprocess.run(["git", *identity, *git_args], cwd=root, check=True)
        config = load_config(root)
        if fake:
            install_fake(config, FakeProvider.from_file(FIX_SCRIPT))
        forge = Forge(config, store=MemoryStore(), root=root)
        async for event in forge.stream(prompt):
            if isinstance(event, ModelDelta):
                print(event.text, end="", flush=True)
            elif isinstance(event, ToolStarted):
                print(f"\n> {event.call.name} {event.call.arguments}")
            elif isinstance(event, SessionDone):
                print(f"\n\n{'done' if event.ok else 'stopped'}:\n{event.report}")
                return 0 if event.ok else 1
    return 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--fake"]
    sys.exit(asyncio.run(main(" ".join(args) or "Make the tests pass", "--fake" in sys.argv)))
