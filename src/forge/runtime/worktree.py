"""Git worktrees for agents, and merging their work back without touching the user's index.

An agent's worktree lives in `.forge/worktrees/<agent>` on branch `forge/<session>/<agent>`,
starting from a snapshot of the main working tree (uncommitted changes included). Merging is
a three-way merge done by `git merge-tree --write-tree` (git 2.38+): the agent's commit
against a fresh snapshot of the main tree, from the last merged base. Only files the merge
changed are written into the main tree; a conflict changes nothing and lists the files.
"""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from forge.runtime.checkpoint import IDENTITY
from forge.runtime.proc import ProcResult, run_argv

GIT_TIMEOUT_S = 120


class WorktreeError(Exception):
    """A git command for a worktree failed."""


@dataclass
class Worktree:
    """One agent's worktree."""

    path: Path
    branch: str
    base: str  # commit the last merge (or the start) was based on


@dataclass
class PreparedMerge:
    """A three-way merge computed in git's object store; nothing written yet."""

    ok: bool
    conflicts: list[str]
    current: str  # snapshot of the main tree the merge is based on
    tree: str  # merged tree (when ok)
    tip: str  # the worktree's commit
    diff: str  # what applying the merge changes in the main tree


async def git(cwd: Path, *args: str, index: Path | None = None, check: bool = True) -> ProcResult:
    """Run git with Forge's identity; raises WorktreeError on failure when `check`."""
    env = dict(IDENTITY)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = await run_argv(["git", *args], cwd, env=env, timeout_s=GIT_TIMEOUT_S)
    if check and result.code != 0:
        raise WorktreeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result


async def tree_commit(cwd: Path, message: str) -> str:
    """A commit of the whole working tree (untracked files too), made through a temp index."""
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index"
        await git(cwd, "add", "-A", ".", index=index)
        tree = (await git(cwd, "write-tree", index=index)).stdout.strip()
    head = await git(cwd, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    parent = ["-p", head.stdout.strip()] if head.code == 0 else []
    result = await git(cwd, "commit-tree", "--no-gpg-sign", tree, *parent, "-m", message)
    return result.stdout.strip()


async def exclude_forge_dir(root: Path) -> None:
    """Keep `.forge/` (worktrees, logs) out of git's view, via .git/info/exclude."""
    common = (await git(root, "rev-parse", "--git-common-dir")).stdout.strip()
    exclude = (root / common / "info" / "exclude").resolve()
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if ".forge/" not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(
            text + ("" if not text or text.endswith("\n") else "\n") + ".forge/\n", encoding="utf-8"
        )


async def create_worktree(root: Path, session_id: str, agent_id: str) -> Worktree:
    """A worktree for the agent, starting from the main tree as it is now."""
    await exclude_forge_dir(root)
    base = await tree_commit(root, f"forge: start of {agent_id}")
    path = root / ".forge" / "worktrees" / agent_id
    branch = f"forge/{session_id}/{agent_id}"
    await git(root, "worktree", "add", "-q", "-b", branch, str(path), base)
    return Worktree(path=path, branch=branch, base=base)


async def commit_worktree(wt: Worktree, message: str) -> str:
    """Commit everything in the worktree on its branch; returns the branch tip."""
    await git(wt.path, "add", "-A", ".")
    staged = await git(wt.path, "diff", "--cached", "--quiet", check=False)
    if staged.code != 0:
        await git(wt.path, "commit", "-q", "--no-gpg-sign", "--no-verify", "-m", message)
    return (await git(wt.path, "rev-parse", "HEAD")).stdout.strip()


async def prepare_merge(root: Path, wt: Worktree, message: str) -> PreparedMerge:
    """Commit the worktree and merge it with a snapshot of the main tree (in memory only)."""
    tip = await commit_worktree(wt, message)
    current = await tree_commit(root, "forge: main tree before merge")
    merged = await git(
        root,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "--merge-base",
        wt.base,
        current,
        tip,
        check=False,
    )
    lines = merged.stdout.splitlines()
    if merged.code == 1:
        conflicts = list(dict.fromkeys(line.strip() for line in lines[1:] if line.strip()))
        return PreparedMerge(False, conflicts, current, "", tip, "")
    if merged.code != 0 or not lines:
        raise WorktreeError(f"git merge-tree failed: {merged.stderr.strip()}")
    tree = lines[0].strip()
    diff = (await git(root, "diff", current, tree)).stdout
    return PreparedMerge(True, [], current, tree, tip, diff)


async def apply_merge(root: Path, wt: Worktree, prepared: PreparedMerge) -> list[str]:
    """Write a clean merge into the main tree; later merges start from this tip."""
    changed = await write_tree_changes(root, prepared.current, prepared.tree)
    wt.base = prepared.tip
    return changed


async def publish_main(root: Path, commit: str, branch: str) -> None:
    """Point `branch` at a main-tree snapshot, so an agent can merge it to resolve a conflict."""
    await git(root, "branch", "-f", branch, commit)


async def write_tree_changes(root: Path, current: str, tree: str) -> list[str]:
    """Make the main tree's files match `tree` where it differs from `current`."""
    listing = await git(root, "diff", "--name-status", "--no-renames", "-z", current, tree)
    fields = [f for f in listing.stdout.split("\0") if f]
    changed: list[str] = []
    for status, name in zip(fields[::2], fields[1::2], strict=True):
        target = root / name
        if status == "D":
            target.unlink(missing_ok=True)
        else:
            blob = await run_bytes(root, f"{tree}:{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        changed.append(name)
    return changed


async def run_bytes(root: Path, spec: str) -> bytes:
    """A blob's exact bytes (run_argv decodes text, which would change binary files)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "cat-file",
        "blob",
        spec,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise WorktreeError(f"git cat-file failed: {err.decode('utf-8', 'replace').strip()}")
    return out


async def remove_worktree(root: Path, wt: Worktree) -> None:
    """Delete the worktree and its branch."""
    await git(root, "worktree", "remove", "--force", str(wt.path), check=False)
    await git(root, "branch", "-D", wt.branch, check=False)
    await git(root, "worktree", "prune", check=False)
