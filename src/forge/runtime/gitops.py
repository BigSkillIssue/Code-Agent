"""Git helpers that read the working tree without changing the user's branch or index."""

from pathlib import Path

from forge.runtime.proc import run_argv, which

MAX_UNTRACKED_BYTES = 20_000


async def is_repo(root: Path) -> bool:
    """True if `root` is inside a git work tree."""
    if which("git") is None:
        return False
    result = await run_argv(["git", "rev-parse", "--is-inside-work-tree"], root)
    return result.code == 0 and result.stdout.strip() == "true"


async def diff_since(root: Path, ref: str | None = None) -> str:
    """Changes in the working tree since `ref` (default HEAD), untracked files included."""
    if not await is_repo(root):
        return "(not a git repository; no diff available)"
    base = ref or "HEAD"
    has_base = (await run_argv(["git", "rev-parse", "--verify", "--quiet", base], root)).code == 0
    tracked = await run_argv(["git", "diff", base] if has_base else ["git", "diff"], root)
    parts = [tracked.stdout.rstrip()]
    untracked = await run_argv(["git", "ls-files", "--others", "--exclude-standard"], root)
    for name in untracked.stdout.splitlines():
        path = root / name
        if path.is_file():
            text = path.read_bytes()[:MAX_UNTRACKED_BYTES].decode("utf-8", "replace")
            body = "\n".join(f"+{line}" for line in text.splitlines())
            parts.append(f"new file: {name}\n{body}")
    return "\n\n".join(p for p in parts if p) or "(no changes)"


async def changed_files(root: Path, ref: str | None = None) -> list[str]:
    """Root-relative paths changed since `ref` (default HEAD), untracked files included."""
    if not await is_repo(root):
        return []
    base = ref or "HEAD"
    has_base = (await run_argv(["git", "rev-parse", "--verify", "--quiet", base], root)).code == 0
    names = await run_argv(
        ["git", "diff", "--name-only", base] if has_base else ["git", "diff", "--name-only"], root
    )
    untracked = await run_argv(["git", "ls-files", "--others", "--exclude-standard"], root)
    found = set(names.stdout.split()) | set(untracked.stdout.split())
    return sorted(p for p in found if not p.startswith(".forge/"))  # Forge's own files
