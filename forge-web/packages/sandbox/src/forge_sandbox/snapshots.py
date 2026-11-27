"""Snapshots of the project's state after a run (W66): the working tree as it is, untracked files
included and ignored ones left out, recorded as a commit under `refs/forge-web/snapshots/<n>`
and packed as a git bundle for the server to keep elsewhere; and a snapshot brought back as a
new branch.

A snapshot never touches the user's branch, HEAD, index or files: it is written through a
temporary index in the transfer folder. Git runs as the workspace owner without hooks, and
nothing here talks to the network.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from forge_sandbox.fsops import Workspace
from forge_sandbox.gitinfo import FORGE_EXCLUDES, GIT_ENV
from forge_sandbox.gitops import TRANSFER, UI_GIT
from forge_sandbox.methods import GitSnapshotParams, GitSnapshotRestoreParams, method
from forge_sandbox.procs import run_program
from forge_sandbox.rpc import Handler, RpcError

PREFIX = "refs/forge-web/snapshots/"
RESTORE = "refs/forge-web/restore/"
BUNDLE = f"{TRANSFER}/snapshot.bundle"
INDEX = f"{TRANSFER}/snapshot.index"
AUTHOR = ["-c", "user.name=Forge Web", "-c", "user.email=snapshots@forge-web.invalid"]
TIMEOUT_S = 600  # a large project takes a while to add and pack
# Forge's working files stay out of snapshots as they stay out of commits (W45: attachments):
# `.git/info/exclude` names them, and they leave the snapshot even when HEAD tracks them.
EXCLUDED = [entry.rstrip("/") for entry in FORGE_EXCLUDES]


class Snapshots:
    """git.snapshot and git.snapshot_restore."""

    def __init__(self, workspace: Workspace, env: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.env = {**env, **GIT_ENV}

    def handlers(self) -> dict[str, Handler]:
        """The methods."""
        return {
            "git.snapshot": method(GitSnapshotParams, self.snapshot),
            "git.snapshot_restore": method(GitSnapshotRestoreParams, self.restore),
        }

    async def _git(
        self, *args: str, check: str = "", env: Mapping[str, str] | None = None
    ) -> tuple[int, str]:
        """Run git; with `check`, a failure is an RpcError whose message starts with it."""
        code, out, err = await run_program(
            [*UI_GIT, *args], self.workspace.root, owner=self.workspace.owner,
            env=env or self.env, timeout=TIMEOUT_S,
        )  # fmt: skip
        if code != 0 and check:
            detail = err.decode("utf-8", "replace").strip()[:500]
            raise RpcError("git_failed", f"{check}: {detail}" if detail else check)
        return code, out.decode("utf-8", "replace").strip()

    async def _resolve(self, name: str) -> str:
        """The object a name points at, or ""."""
        code, out = await self._git("rev-parse", "--verify", "-q", name)
        return out if code == 0 else ""

    async def snapshot(self, params: GitSnapshotParams) -> dict[str, Any]:
        """Record the working tree; `unchanged` when it is the previous snapshot's."""
        if (await self._git("rev-parse", "--git-dir"))[0] != 0:
            raise RpcError("no_repo", "the project is not a git repository")
        await asyncio.to_thread(self.workspace.mkdir, TRANSFER)
        tree = await self._tree()
        previous = await self._resolve(f"{params.previous}^{{commit}}") if params.previous else ""
        if previous and await self._resolve(f"{previous}^{{tree}}") == tree:
            return {"unchanged": True, "ref": params.previous, "commit": previous, "tree": tree}
        head = await self._resolve("HEAD^{commit}")
        parents = ["-p", head] if head else []
        message = ["-m", "Forge Web snapshot"]
        _, commit = await self._git(*AUTHOR, "commit-tree", tree, *parents, *message,
                                    check="could not record the snapshot")  # fmt: skip
        ref = PREFIX + str(await self._next_number())
        await self._git("update-ref", ref, commit, check="could not keep the snapshot")
        full = params.full or not previous
        await self._pack(ref, "" if full else previous)
        await self._prune({ref, params.previous or ""})
        size = (await asyncio.to_thread(self.workspace.stat, BUNDLE)).get("size", 0)
        return {"unchanged": False, "ref": ref, "commit": commit, "tree": tree, "full": full,
                "bundle": BUNDLE, "size": size}  # fmt: skip

    async def _tree(self) -> str:
        """The working tree as a git tree, written through an index of its own."""
        index = self.workspace.root / INDEX
        await asyncio.to_thread(self._delete, INDEX)
        env = {**self.env, "GIT_INDEX_FILE": str(index)}
        try:
            if await self._resolve("HEAD^{commit}"):  # tracked files stay, even ignored ones
                await self._git("read-tree", "HEAD", env=env, check="could not read HEAD")
            await self._git("add", "-A", "--", ".", env=env, check="could not record the files")
            await self._git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *EXCLUDED,
                            env=env, check="could not leave out Forge's files")  # fmt: skip
            return (await self._git("write-tree", env=env, check="could not write the tree"))[1]
        finally:
            await asyncio.to_thread(self._delete, INDEX)

    async def _next_number(self) -> int:
        """One more than the highest snapshot number in the repository."""
        _, out = await self._git("for-each-ref", "--format=%(refname)", PREFIX)
        numbers = [int(line[len(PREFIX) :]) for line in out.splitlines()
                   if line[len(PREFIX) :].isdigit()]  # fmt: skip
        return max(numbers, default=0) + 1

    async def _pack(self, ref: str, previous: str) -> None:
        """The bundle of the new snapshot: everything, or what is new since `previous`."""
        await asyncio.to_thread(self._delete, BUNDLE)
        since = [f"^{previous}"] if previous else []
        await self._git("bundle", "create", "-q", BUNDLE, ref, *since,
                        check="could not pack the snapshot")  # fmt: skip

    async def _prune(self, keep: set[str]) -> None:
        """Drop older snapshot refs: the server keeps them, the next one builds on the newest."""
        _, out = await self._git("for-each-ref", "--format=%(refname)", PREFIX)
        for name in out.splitlines():
            if name not in keep:
                await self._git("update-ref", "-d", name)

    async def restore(self, params: GitSnapshotRestoreParams) -> dict[str, Any]:
        """Read the chain's bundles and make a new branch at the snapshot's commit."""
        names = [f"{TRANSFER}/{name}" for name in params.bundles]
        try:
            for bundle in names:
                await self._git("fetch", "-q", "--no-tags", bundle,
                                f"+{PREFIX}*:{RESTORE}*",
                                check="could not read the snapshot")  # fmt: skip
            if not await self._resolve(f"{params.commit}^{{commit}}"):
                raise RpcError("not_found", "the snapshot's commit is not in what was sent")
            branch = await self._free_branch(params.branch)
            await self._git("branch", branch, params.commit, check="could not make the branch")
        finally:
            for bundle in names:
                await asyncio.to_thread(self._delete, bundle)
            await self._drop_restore_refs()
        return {"branch": branch, "commit": params.commit}

    async def _free_branch(self, wanted: str) -> str:
        """`wanted`, or with -2, -3, … when a branch of that name exists already."""
        name, number = wanted, 1
        while await self._resolve(f"refs/heads/{name}"):
            number += 1
            name = f"{wanted}-{number}"
        return name

    async def _drop_restore_refs(self) -> None:
        _, out = await self._git("for-each-ref", "--format=%(refname)", RESTORE)
        for name in out.splitlines():
            await self._git("update-ref", "-d", name)

    def _delete(self, rel: str) -> None:
        try:
            self.workspace.delete(rel)
        except RpcError as err:
            if err.code != "not_found":
                raise
