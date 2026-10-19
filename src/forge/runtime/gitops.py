"""Git helpers that read the working tree without changing the user's branch or index.

Forge runs git outside the command sandbox, but sandboxed commands can write `.git/config`
(and `.git/info/attributes`), which can name programs git runs: hooks, the fsmonitor, clean
and smudge filters, diff and merge drivers. `run_git` switches all of them off.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from forge.runtime.proc import ProcResult, run_argv, which

MAX_UNTRACKED_BYTES = 20_000
# Tool caches are not changes anyone made (a project without .gitignore shows them).
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
CACHE_PATHSPECS = tuple(f":(exclude,glob)**/{name}/**" for name in sorted(CACHE_DIRS))


async def is_repo(root: Path) -> bool:
    """True if `root` is inside a git work tree."""
    if which("git") is None:
        return False
    result = await run_git(root, "rev-parse", "--is-inside-work-tree")
    return result.code == 0 and result.stdout.strip() == "true"


async def diff_since(root: Path, ref: str | None = None) -> str:
    """Changes in the working tree since `ref` (default HEAD), untracked files included."""
    if not await is_repo(root):
        return "(not a git repository; no diff available)"
    base = ref or "HEAD"
    has_base = (await run_git(root, "rev-parse", "--verify", "--quiet", base)).code == 0
    command = ["diff", "--no-ext-diff", "--no-textconv", *([base] if has_base else [])]
    tracked = await run_git(root, *command, "--", ".", *CACHE_PATHSPECS)
    parts = [tracked.stdout.rstrip()]
    untracked = await run_git(root, "ls-files", "--others", "--exclude-standard")
    for name in untracked.stdout.splitlines():
        path = root / name
        if path.is_symlink():  # a link is shown as one, never read: it may point outside
            parts.append(f"new link: {name} -> {os.readlink(path)}")
        elif path.is_file() and not is_cache(name):
            text = path.read_bytes()[:MAX_UNTRACKED_BYTES].decode("utf-8", "replace")
            body = "\n".join(f"+{line}" for line in text.splitlines())
            parts.append(f"new file: {name}\n{body}")
    return "\n\n".join(p for p in parts if p) or "(no changes)"


async def changed_files(root: Path, ref: str | None = None) -> list[str]:
    """Root-relative paths changed since `ref` (default HEAD), untracked files included."""
    if not await is_repo(root):
        return []
    base = ref or "HEAD"
    has_base = (await run_git(root, "rev-parse", "--verify", "--quiet", base)).code == 0
    names = await run_git(root, "diff", "--name-only", *([base] if has_base else []))
    untracked = await run_git(root, "ls-files", "--others", "--exclude-standard")
    found = set(names.stdout.split()) | set(untracked.stdout.split())
    return sorted(p for p in found if not is_cache(p) and not p.startswith(".forge/"))


# Settings that name a program, set to nothing (or to a harmless value) on git's command line,
# which beats every config file.
SAFE_SETTINGS = (
    "core.fsmonitor=false",
    f"core.hooksPath={os.devnull}",
    f"core.attributesFile={os.devnull}",
    "core.pager=cat",
    "core.sshCommand=",
    "core.askPass=",
    "credential.helper=",
    "diff.external=",
    "gpg.program=",
    "uploadpack.packObjectsHook=",
)
# Drivers are named by the config ("filter.<name>.clean"), so each one found is switched off.
DRIVER_KEYS = r"^(filter|diff|merge)\..+\.(clean|smudge|process|textconv|command|driver)$"


def safe_options() -> list[str]:
    """`-c key=value` for every fixed setting in SAFE_SETTINGS."""
    return [word for setting in SAFE_SETTINGS for word in ("-c", setting)]


async def driver_options(root: Path) -> list[str]:
    """`-c key=` for every filter, diff and merge driver the repository's config names."""
    listed = await run_argv(
        ["git", *safe_options(), "config", "--name-only", "--get-regexp", DRIVER_KEYS], root
    )
    options: list[str] = []
    for key in listed.stdout.split():
        if re.fullmatch(DRIVER_KEYS, key):
            options += ["-c", f"{key}="]
        if key.startswith("filter."):
            options += ["-c", f"{key.rsplit('.', 1)[0]}.required=false"]
    return options


async def run_git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 60,
) -> ProcResult:
    """Run git in `root` without any program the repository's own config names."""
    argv = ["git", *safe_options(), *await driver_options(root), *args]
    return await run_argv(argv, root, env=env, timeout_s=timeout_s)


def is_cache(path: str) -> bool:
    """True for files inside a tool cache folder (`__pycache__/x.pyc`)."""
    return bool(CACHE_DIRS & set(path.split("/")))
