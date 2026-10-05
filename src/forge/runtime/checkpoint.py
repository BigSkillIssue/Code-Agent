"""Working-tree snapshots in hidden git refs, so /undo can roll back one step.

Snapshots go through a temporary index, so the user's branch, HEAD and staged changes
are never touched. Refs: refs/forge/<session>/<step>.
"""

import tempfile
from pathlib import Path

from forge.runtime.gitops import is_repo
from forge.runtime.proc import ProcResult, run_argv

IDENTITY = {
    "GIT_AUTHOR_NAME": "Forge",
    "GIT_AUTHOR_EMAIL": "forge@localhost",
    "GIT_COMMITTER_NAME": "Forge",
    "GIT_COMMITTER_EMAIL": "forge@localhost",
}


class CheckpointError(Exception):
    """A git command needed for a snapshot failed."""


def ref_name(session_id: str, step_id: str) -> str:
    """The hidden ref of one step's snapshot."""
    return f"refs/forge/{session_id}/{step_id}"


async def _git(root: Path, *args: str, index: Path | None = None) -> ProcResult:
    env = dict(IDENTITY)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = await run_argv(["git", *args], root, env=env)
    if result.code != 0:
        raise CheckpointError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


async def snapshot(root: Path, session_id: str, step_id: str) -> str | None:
    """Record the whole working tree (untracked files too) under a hidden ref; None outside git."""
    if not await is_repo(root):
        return None
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index"
        await _git(root, "add", "-A", ".", index=index)
        tree = (await _git(root, "write-tree", index=index)).stdout.strip()
    head = await run_argv(["git", "rev-parse", "--verify", "--quiet", "HEAD"], root)
    parent = ["-p", head.stdout.strip()] if head.code == 0 else []
    message = f"forge checkpoint {session_id} {step_id}"
    commit = (
        await _git(root, "commit-tree", "--no-gpg-sign", tree, *parent, "-m", message)
    ).stdout.strip()
    ref = ref_name(session_id, step_id)
    await _git(root, "update-ref", ref, commit)
    return ref


async def restore(root: Path, ref: str) -> list[str]:
    """Make the working tree equal to the snapshot; returns the paths that changed or vanished."""
    with tempfile.TemporaryDirectory() as folder:
        now_index, then_index = Path(folder) / "now", Path(folder) / "then"
        await _git(root, "add", "-A", ".", index=now_index)
        current = set((await _git(root, "ls-files", "-z", index=now_index)).stdout.split("\0")) - {
            ""
        }
        await _git(root, "read-tree", ref, index=then_index)
        wanted = set((await _git(root, "ls-files", "-z", index=then_index)).stdout.split("\0")) - {
            ""
        }
        diff = await _git(root, "diff-index", "--name-only", "-z", ref, index=now_index)
        changed = set(diff.stdout.split("\0")) - {""}
        for name in sorted(current - wanted):
            (root / name).unlink(missing_ok=True)
            _remove_empty_parents(root, root / name)
        await _git(root, "checkout-index", "-a", "-f", index=then_index)
    return sorted(changed | (current - wanted))


def _remove_empty_parents(root: Path, path: Path) -> None:
    folder = path.parent
    while folder != root and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


async def drop_refs(root: Path, session_id: str) -> None:
    """Delete every snapshot ref of a session."""
    listing = await run_argv(
        ["git", "for-each-ref", "--format=%(refname)", f"refs/forge/{session_id}/"], root
    )
    for ref in listing.stdout.split():
        await run_argv(["git", "update-ref", "-d", ref], root)
