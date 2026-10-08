"""Changes people make to the workspace's repository in the web UI: stage, unstage, discard,
commit, branches, the remote, and the bundles that carry commits to and from the git job.

Git runs as the workspace owner. Repository hooks never run for these actions
(`core.hooksPath=/dev/null`), and nothing here talks to the network.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from forge_sandbox.fsops import Workspace, split_path
from forge_sandbox.gitinfo import GIT_ENV, SAFE_GIT
from forge_sandbox.methods import (
    EmptyParams,
    GitCommitParams,
    GitLogParams,
    GitPathsParams,
    GitRemoteParams,
    GitSwitchParams,
    method,
)
from forge_sandbox.procs import run_program
from forge_sandbox.rpc import Handler, RpcError

UI_GIT = [*SAFE_GIT, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false"]
LOG_FORMAT = "%H%x00%an%x00%ae%x00%at%x00%s"


def git_paths(paths: list[str]) -> list[str]:
    """Workspace-relative paths for git (refuses anything that leaves the workspace)."""
    return ["/".join(split_path(p)) or "." for p in paths]


class GitOps:
    """git.stage, git.unstage, git.discard, git.commit, git.branches, git.switch, git.log,
    git.remote and git.set_remote."""

    def __init__(self, workspace: Workspace, env: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.env = {**env, **GIT_ENV}

    def handlers(self) -> dict[str, Handler]:
        """The methods."""
        return {
            "git.stage": method(GitPathsParams, self.stage),
            "git.unstage": method(GitPathsParams, self.unstage),
            "git.discard": method(GitPathsParams, self.discard),
            "git.commit": method(GitCommitParams, self.commit),
            "git.branches": method(EmptyParams, self.branches),
            "git.switch": method(GitSwitchParams, self.switch),
            "git.log": method(GitLogParams, self.log),
            "git.remote": method(EmptyParams, self.remote),
            "git.set_remote": method(GitRemoteParams, self.set_remote),
        }

    async def _git(self, *args: str, check: str = "") -> str:
        """Run git; with `check`, a failure is an RpcError whose message starts with it."""
        code, out, err = await run_program(
            [*UI_GIT, *args],
            self.workspace.root,
            owner=self.workspace.owner,
            env=self.env,
            timeout=120,
        )
        if code != 0 and check:
            detail = err.decode("utf-8", "replace").strip()[:500]
            raise RpcError("git_failed", f"{check}: {detail}" if detail else check)
        return out.decode("utf-8", "replace") if code == 0 else ""

    async def _has_commits(self) -> bool:
        code, _, _ = await run_program(
            [*UI_GIT, "rev-parse", "--verify", "-q", "HEAD"],
            self.workspace.root,
            owner=self.workspace.owner,
            env=self.env,
        )
        return code == 0

    async def stage(self, params: GitPathsParams) -> dict[str, Any]:
        """Add changes (new, changed and deleted files) to the next commit."""
        await self._git("add", "-A", "--", *git_paths(params.paths), check="could not stage")
        return {"ok": True}

    async def unstage(self, params: GitPathsParams) -> dict[str, Any]:
        """Take changes out of the next commit (the files keep them)."""
        paths = git_paths(params.paths)
        if await self._has_commits():
            await self._git("restore", "--staged", "--", *paths, check="could not unstage")
        else:
            await self._git("rm", "-r", "-q", "--cached", "--", *paths, check="could not unstage")
        return {"ok": True}

    async def discard(self, params: GitPathsParams) -> dict[str, Any]:
        """Throw away the changes of these paths: tracked files go back to the last commit,
        new files are deleted."""
        paths = git_paths(params.paths)
        tracked = set((await self._git("ls-files", "-z", "--", *paths)).split("\0")) - {""}
        if tracked and await self._has_commits():
            await self._git("restore", "--source=HEAD", "--staged", "--worktree", "--",
                            *sorted(tracked), check="could not discard")  # fmt: skip
        for path in paths:
            if path not in tracked and not any(t.startswith(f"{path}/") for t in tracked):
                await asyncio.to_thread(self._delete_new, path)
        return {"ok": True}

    def _delete_new(self, path: str) -> None:
        try:
            self.workspace.delete(path, recursive=True)
        except RpcError as err:
            if err.code != "not_found":
                raise

    async def commit(self, params: GitCommitParams) -> dict[str, Any]:
        """Commit what is staged, as the signed-in person."""
        staged = await self._git("diff", "--cached", "--name-only", "-z")
        if not staged.strip("\0") and await self._has_commits():
            raise RpcError("nothing_staged", "nothing is staged for the commit")
        await self._git(
            "-c", f"user.name={params.name}", "-c", f"user.email={params.email}",
            "commit", "-q", "--no-verify", "-m", params.message, check="could not commit",
        )  # fmt: skip
        head = (await self._git("rev-parse", "HEAD")).strip()
        return {"commit": head}

    async def branches(self, _params: EmptyParams) -> dict[str, Any]:
        """Local branches and the current one."""
        raw = await self._git(
            "for-each-ref", "--format=%(refname:short)%00%(objectname:short)%00%(upstream:short)",
            "refs/heads",
        )  # fmt: skip
        found = []
        for line in raw.splitlines():
            name, _, rest = line.partition("\0")
            commit, _, upstream = rest.partition("\0")
            found.append({"name": name, "commit": commit, "upstream": upstream or None})
        current = (await self._git("symbolic-ref", "--short", "-q", "HEAD")).strip() or None
        return {"current": current, "branches": found}

    async def switch(self, params: GitSwitchParams) -> dict[str, Any]:
        """Switch to a branch (creating it from the current commit with `create`)."""
        branch = params.branch.strip()
        await self._git("check-ref-format", "--branch", branch, check="not a valid branch name")
        args = ["switch", "-q", *(["-c"] if params.create else []), branch]
        await self._git(*args, check=f"could not switch to {branch}")
        return {"current": branch}

    async def log(self, params: GitLogParams) -> dict[str, Any]:
        """The latest commits of the current branch."""
        if not await self._has_commits():
            return {"commits": []}
        raw = await self._git("log", f"-n{params.limit}", f"--format={LOG_FORMAT}")
        commits = []
        for line in raw.splitlines():
            sha, author, email, when, subject = (line.split("\0") + [""] * 5)[:5]
            commits.append({"commit": sha, "author": author, "email": email,
                            "time": int(when or 0), "subject": subject})  # fmt: skip
        return {"commits": commits}

    async def remote(self, _params: EmptyParams) -> dict[str, Any]:
        """The URL of `origin`, if there is one."""
        url = (await self._git("remote", "get-url", "origin")).strip()
        return {"url": url or None}

    async def set_remote(self, params: GitRemoteParams) -> dict[str, Any]:
        """Point `origin` at a URL (the server checks the URL first)."""
        exists = bool((await self._git("remote", "get-url", "origin")).strip())
        action = "set-url" if exists else "add"
        await self._git("remote", action, "origin", params.url, check="could not set the remote")
        return {"url": params.url}
