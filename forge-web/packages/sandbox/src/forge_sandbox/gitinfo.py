"""Read-only git questions about the workspace: status and diffs.

Git runs as the workspace owner with settings that keep it from starting other programs
(fsmonitor, external diff drivers, text conversion).
"""

from collections.abc import Mapping
from typing import Any

from forge_sandbox.fsops import Workspace, split_path
from forge_sandbox.methods import EmptyParams, GitDiffParams, method
from forge_sandbox.procs import run_program
from forge_sandbox.rpc import Handler, RpcError

SAFE_GIT = ["git", "-c", "core.fsmonitor=", "-c", "color.ui=false", "-c", "core.quotepath=false"]
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}


def parse_branch(header: str) -> dict[str, Any]:
    """`## main...origin/main [ahead 1, behind 2]` as fields."""
    text = header.removeprefix("## ")
    info: dict[str, Any] = {"branch": None, "upstream": None, "ahead": 0, "behind": 0}
    names, _, counts = text.partition(" [")
    if names.startswith("No commits yet on "):
        info["branch"] = names.removeprefix("No commits yet on ")
        return info
    branch, _, upstream = names.partition("...")
    info["branch"] = None if branch.startswith("HEAD (no branch)") else branch
    info["upstream"] = upstream or None
    for part in counts.rstrip("]").split(", "):
        key, _, number = part.partition(" ")
        if key in ("ahead", "behind") and number.isdigit():
            info[key] = int(number)
    return info


def parse_status(raw: str) -> dict[str, Any]:
    """`git status --porcelain=v1 -z --branch` output as branch info plus changed files."""
    entries = raw.split("\0")
    info: dict[str, Any] = {"branch": None, "upstream": None, "ahead": 0, "behind": 0}
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if entry.startswith("## "):
            info = parse_branch(entry)
            continue
        code, path = entry[:2], entry[3:]
        item: dict[str, Any] = {"path": path, "index": code[0], "worktree": code[1], "from": None}
        if code[0] in "RC" and index < len(entries):
            item["from"] = entries[index]
            index += 1
        files.append(item)
    return {**info, "files": files}


class GitInfo:
    """git.status and git.diff."""

    def __init__(self, workspace: Workspace, env: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.env = {**env, **GIT_ENV}

    def handlers(self) -> dict[str, Handler]:
        """git.* methods."""
        return {
            "git.status": method(EmptyParams, self.status),
            "git.diff": method(GitDiffParams, self.diff),
        }

    async def _git(self, *args: str, timeout: float = 30) -> tuple[int, str, str]:
        code, out, err = await run_program(
            [*SAFE_GIT, *args],
            self.workspace.root,
            owner=self.workspace.owner,
            env=self.env,
            timeout=timeout,
        )
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def status(self, _params: EmptyParams) -> dict[str, Any]:
        """Branch, upstream and changed files; `repo: false` outside a repository."""
        code, out, _ = await self._git("rev-parse", "--is-inside-work-tree")
        if code != 0 or out.strip() != "true":
            return {"repo": False, "files": []}
        code, out, err = await self._git(
            "status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all"
        )
        if code != 0:
            raise RpcError("git_failed", err.strip()[:500] or "git status failed")
        return {"repo": True, **parse_status(out)}

    async def diff(self, params: GitDiffParams) -> dict[str, Any]:
        """A unified diff of the working tree (or the index with `staged`)."""
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if params.staged:
            args.append("--cached")
        if params.path:
            args += ["--", "/".join(split_path(params.path))]
        code, out, err = await self._git(*args)
        if code != 0:
            raise RpcError("git_failed", err.strip()[:500] or "git diff failed")
        truncated = len(out) > params.limit
        return {"diff": out[: params.limit], "truncated": truncated}
