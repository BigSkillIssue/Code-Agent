"""Read-only git questions about the workspace: status and diffs.

Git runs as the workspace owner with settings that keep it from starting other programs
(fsmonitor, external diff drivers, text conversion).
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from forge_sandbox.fsops import Workspace, split_path
from forge_sandbox.methods import EmptyParams, GitDiffParams, GitFilesParams, method
from forge_sandbox.procs import run_program
from forge_sandbox.rpc import Handler, RpcError

MAX_FILES = 50_000  # files git.files looks at
SAFE_GIT = ["git", "-c", "core.fsmonitor=", "-c", "color.ui=false", "-c", "core.quotepath=false"]
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
# Forge's own working files in a project: kept out of commits (in .git/info/exclude, which is
# never committed). The project's Forge config (.forge/config.toml, commands, agents, skills)
# stays visible to git.
EXCLUDE_HEADER = "# Forge's working files (added by Forge Web)"
FORGE_EXCLUDES = (".forge/audit.log", ".forge/undo/", ".forge/out/", ".forge/cache/",
                  ".forge/worktrees/")  # fmt: skip


def match_rank(path: str, query: str) -> int | None:
    """How well a path matches a search (lower is better), or None. Case does not matter."""
    lower, name = path.lower(), path.lower().rsplit("/", 1)[-1]
    if not query:
        return 0
    if name.startswith(query):
        return 0
    if query in name:
        return 1
    if query in lower:
        return 2
    position = 0
    for char in query:  # the letters in order, with gaps
        position = lower.find(char, position) + 1
        if position == 0:
            return None
    return 3


def best_matches(paths: list[str], query: str, limit: int) -> list[str]:
    """The paths that match, best first (then shorter, then alphabetical)."""
    query = query.strip().lower()
    ranked = [(rank, len(p), p) for p in paths if (rank := match_rank(p, query)) is not None]
    return [p for _rank, _length, p in sorted(ranked)[:limit]]


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
            "git.init": method(EmptyParams, self.init),
            "git.files": method(GitFilesParams, self.files),
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

    async def init(self, _params: EmptyParams) -> dict[str, Any]:
        """Make the workspace a git repository on branch main (unless it is one) and keep
        Forge's working files out of its commits."""
        code, out, _ = await self._git("rev-parse", "--is-inside-work-tree")
        created = not (code == 0 and out.strip() == "true")
        if created:
            code, _, err = await self._git("init", "-q", "-b", "main")
            if code != 0:
                raise RpcError("git_failed", err.strip()[:500] or "git init failed")
        await asyncio.to_thread(self._exclude_forge_files)
        return {"created": created}

    def _exclude_forge_files(self) -> None:
        path = ".git/info/exclude"
        try:
            current = self.workspace.read(path).get("text") or ""
        except RpcError as err:
            if err.code != "not_found":
                return  # a link or something odd: leave it alone
            current = ""
        missing = [line for line in FORGE_EXCLUDES if line not in current.splitlines()]
        if missing:
            block = "\n".join([EXCLUDE_HEADER, *missing]) + "\n"
            text = current + ("" if current.endswith("\n") or not current else "\n") + block
            self.workspace.write(path, text.encode("utf-8"), create_dirs=True)

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

    async def files(self, params: GitFilesParams) -> dict[str, Any]:
        """Tracked and untracked files that are not ignored; empty outside a repository."""
        code, out, _ = await self._git(
            "ls-files", "--cached", "--others", "--exclude-standard", "--deduplicate", "-z"
        )
        paths = [p for p in out.split("\0") if p][:MAX_FILES] if code == 0 else []
        return {"files": best_matches(paths, params.query, params.limit), "total": len(paths)}
