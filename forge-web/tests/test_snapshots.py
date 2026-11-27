"""The project's state after every run (W66): the sandbox records the working tree as a snapshot
commit and packs it as a git bundle without touching the user's branch, HEAD or index; the
server joins runs within the debounce, refuses what is too big or not a bundle, seals it with the
project's key and keeps it on a storage node (or on its own disk without one); retention keeps
whole chains; a snapshot comes back as a new branch, for owners and editors only."""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_hostworker.blobs import BlobStore
from forge_hostworker.client import HostClient
from forge_sandbox.rpc import RpcError
from forge_web.db.engine import Database
from forge_web.db.models import AuditEntry, Project, ProjectMember, User
from forge_web.db.snapshot_models import ProjectSnapshot
from forge_web.hosting import deploy_api
from forge_web.hosting.hosts import HostQueue, create_host
from forge_web.settings import WebSettings
from forge_web.snapshots.queue import SnapshotQueue
from forge_web.snapshots.settings import SnapshotSettings
from forge_web.snapshots.store import SnapshotStore, kept
from forge_web.vault import Vault
from host_standin import HostStandIn
from support import LiveServer, call, dev_settings, fake_script, person, sandbox


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def repo(root: Path) -> None:
    """A repository with one commit, an ignore file and Forge's excludes."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / ".gitignore").write_text("build/\n*.log\n")
    (root / "app.py").write_text("a = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")


def tree_files(root: Path, commit: str) -> set[str]:
    return set(git(root, "ls-tree", "-r", "--name-only", commit).splitlines())


async def test_a_snapshot_has_untracked_files_and_leaves_the_users_repository(
    tmp_path: Path,
) -> None:
    repo(tmp_path)
    (tmp_path / "app.py").write_text("a = 2\n")  # changed, not staged
    (tmp_path / "new.txt").write_text("new\n")  # untracked
    (tmp_path / "staged.txt").write_text("staged\n")
    git(tmp_path, "add", "staged.txt")
    (tmp_path / "debug.log").write_text("ignored\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_text("ignored\n")
    (tmp_path / ".forge" / "attachments").mkdir(parents=True)
    (tmp_path / ".forge" / "attachments" / "photo.png").write_text("upload\n")
    before = (git(tmp_path, "rev-parse", "HEAD"), git(tmp_path, "status", "--porcelain"),
              (tmp_path / ".git" / "index").read_bytes())  # fmt: skip
    async with sandbox(tmp_path) as (_daemon, client):
        made = await client.call("git.snapshot", {})
    assert made["unchanged"] is False and made["full"] is True
    assert made["ref"] == "refs/forge-web/snapshots/1" and made["size"] > 0
    assert (tmp_path / made["bundle"]).is_file()
    files = tree_files(tmp_path, made["commit"])
    assert files == {".gitignore", "app.py", "new.txt", "staged.txt"}
    assert git(tmp_path, "show", f"{made['commit']}:app.py") == "a = 2"
    assert git(tmp_path, "rev-parse", f"{made['commit']}^") == before[0]
    after = (git(tmp_path, "rev-parse", "HEAD"), git(tmp_path, "status", "--porcelain"),
             (tmp_path / ".git" / "index").read_bytes())  # fmt: skip
    assert after == before  # HEAD, the index and what git status says are the same
    assert git(tmp_path, "branch", "--format=%(refname:short)") == "main"
    assert not (tmp_path / ".git" / "forge-transfer" / "snapshot.index").exists()


async def test_an_unchanged_tree_makes_no_snapshot_and_the_next_is_incremental(
    tmp_path: Path,
) -> None:
    repo(tmp_path)
    async with sandbox(tmp_path) as (_daemon, client):
        first = await client.call("git.snapshot", {})
        same = await client.call("git.snapshot", {"previous": first["ref"]})
        assert same == {"unchanged": True, "ref": first["ref"], "commit": first["commit"],
                        "tree": first["tree"]}  # fmt: skip
        (tmp_path / "big.txt").write_text("x" * 50_000)
        second = await client.call("git.snapshot", {"previous": first["ref"]})
        assert second["ref"] == "refs/forge-web/snapshots/2" and second["full"] is False
        full = await client.call("git.snapshot", {"previous": second["ref"], "full": True})
    assert full["unchanged"] is True  # nothing changed since the second
    bundle = tmp_path / second["bundle"]
    heads = git(tmp_path, "bundle", "list-heads", str(bundle))
    assert heads.endswith("refs/forge-web/snapshots/2")
    verified = subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=tmp_path,
                              capture_output=True, text=True)  # fmt: skip
    assert verified.returncode == 0 and "requires this ref" in verified.stdout  # incremental


async def test_a_previous_ref_the_repository_lost_gives_a_full_bundle(tmp_path: Path) -> None:
    repo(tmp_path)
    async with sandbox(tmp_path) as (_daemon, client):
        made = await client.call("git.snapshot", {"previous": "refs/forge-web/snapshots/7"})
        assert made["full"] is True and made["ref"] == "refs/forge-web/snapshots/1"
        with pytest.raises(RpcError):
            await client.call("git.snapshot", {"previous": "refs/heads/main"})


async def test_restore_makes_a_new_branch_from_the_chain(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    repo(source)
    (source / "keep.txt").write_text("in both snapshots, packed only in the first\n" * 100)
    bundles: list[bytes] = []
    async with sandbox(source) as (_daemon, client):
        first = await client.call("git.snapshot", {})
        bundles.append((source / first["bundle"]).read_bytes())
        (source / "notes.md").write_text("untracked notes\n")
        second = await client.call("git.snapshot", {"previous": first["ref"]})
        bundles.append((source / second["bundle"]).read_bytes())
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    transfer = target / ".git" / "forge-transfer"
    transfer.mkdir()
    names = [f"snap-{str(n) * 32}.bundle" for n in (1, 2)]
    for name, data in zip(names, bundles, strict=True):
        (transfer / name).write_bytes(data)
    params: dict[str, Any] = {"bundles": names, "commit": second["commit"],
                              "branch": "restore/2026-11-27-0930"}  # fmt: skip
    async with sandbox(target) as (_daemon, client):
        made = await client.call("git.snapshot_restore", params)
        assert made["branch"] == "restore/2026-11-27-0930"
        for name, data in zip(names, bundles, strict=True):
            (transfer / name).write_bytes(data)
        again = await client.call("git.snapshot_restore", params)
        assert again["branch"] == "restore/2026-11-27-0930-2"  # never overwrites a branch
        with pytest.raises(RpcError):
            await client.call("git.snapshot_restore", {**params, "bundles": ["../../x.bundle"]})
    assert git(target, "rev-parse", "restore/2026-11-27-0930^{tree}") == second["tree"]
    assert git(target, "show", "restore/2026-11-27-0930:notes.md") == "untracked notes"
    assert len(bundles[1]) < len(bundles[0])
    assert list(transfer.iterdir()) == []  # the bundles are gone
    assert git(target, "for-each-ref", "refs/forge-web/restore") == ""
    assert git(target, "symbolic-ref", "--short", "HEAD") == "main"


# The server's side: the queue with a real sandbox daemon in this process. -------------------

PID = "0123456789abcdef"
NOW = 1_790_000_000.0


@dataclass
class Queued:
    """A snapshot queue on a fresh database, calling a real daemon in `root`."""

    queue: SnapshotQueue
    store: SnapshotStore
    db: Database
    root: Path
    calls: list[str]


@pytest.fixture
async def queued(tmp_path: Path, settings: WebSettings) -> AsyncIterator[Queued]:
    root = tmp_path / "workspace"
    root.mkdir()
    repo(root)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'snap.db'}")
    await db.migrate()
    now = time.time()
    async with db.session() as session, session.begin():
        session.add(User(id="u1", email="ada@example.com", created_at=now))
        await session.flush()
        session.add(Project(id=PID, name="Shop", owner_id="u1", created_at=now, updated_at=now))
    settings.snapshots.debounce_s = 0.2
    store = SnapshotStore(db, Vault(b"m" * 32), settings, HostQueue(db, settings.data_dir, 60))
    calls: list[str] = []
    async with sandbox(root) as (_daemon, client):

        async def call(_project: str, name: str, params: dict[str, Any]) -> Any:
            calls.append(name)
            return await client.call(name, params)

        queue = SnapshotQueue(db, settings, store, call, lambda _project: True)
        yield Queued(queue, store, db, root, calls)
        await queue.close()
    await db.close()


async def rows(db: Database, *states: str) -> list[ProjectSnapshot]:
    async with db.session() as session:
        found = await session.scalars(select(ProjectSnapshot).order_by(ProjectSnapshot.created_at))
        return [row for row in found if not states or row.state in states]


async def test_runs_within_the_debounce_make_one_snapshot(queued: Queued) -> None:
    for _ in range(3):  # three runs end close together
        queued.queue.request(PID, "chat-1")
        await asyncio.sleep(0.05)
    async with asyncio.timeout(10):
        while queued.calls.count("git.snapshot") < 1 or queued.queue.tasks:
            await asyncio.sleep(0.05)
    made = await rows(queued.db)
    assert len(made) == 1 and made[0].kind == "full" and made[0].chat_id == "chat-1"
    assert made[0].state == "stored" and made[0].node_id is None  # no storage node: kept here
    sealed = queued.store.path(PID, made[0].id)
    assert b"git bundle" not in sealed.read_bytes()  # sealed with the project's key
    plain = b"".join([part async for part in queued.store.plain_parts(PID, sealed)])
    assert plain.startswith((b"# v2 git bundle", b"# v3 git bundle"))
    (queued.root / "more.txt").write_text("more\n")
    second = await queued.queue.snapshot(PID, "chat-2")
    assert second is not None and second.kind == "incr" and second.parent_id == made[0].id
    assert await queued.queue.snapshot(PID, "chat-2") is None  # unchanged: no snapshot
    assert len(await rows(queued.db)) == 2


async def test_a_sandbox_that_is_not_running_is_left_alone(queued: Queued) -> None:
    queued.queue.running = lambda _project: False
    assert await queued.queue.snapshot(PID) is None
    assert queued.calls == [] and await rows(queued.db) == []


class Hostile:
    """The sandbox, but it lies about the snapshot's ref once and then sends no bundle."""

    def __init__(self, call: Any) -> None:
        self.call, self.lies = call, 0

    async def __call__(self, project: str, name: str, params: dict[str, Any]) -> Any:
        if name == "fs.read" and self.lies > 1:
            return {"text": "#!/bin/sh\nnot a bundle\n", "truncated": False}
        answer = await self.call(project, name, params)
        if name == "git.snapshot" and isinstance(answer, dict) and "size" in answer:
            self.lies += 1
            if self.lies == 1:
                answer = {**answer, "ref": "../../etc/passwd"}
        return answer


async def test_too_big_or_broken_bundles_are_refused(queued: Queued) -> None:
    queued.queue.settings.snapshots.max_mb = 1
    (queued.root / "video.bin").write_bytes(os.urandom(1_500_000))
    assert await queued.queue.snapshot(PID) is None
    failed = await rows(queued.db, "failed")
    assert len(failed) == 1 and "larger than 1 MB" in failed[0].error
    assert not list(queued.store.root.rglob("*")) or not any(
        p.is_file() for p in queued.store.root.rglob("*")
    )
    (queued.root / "video.bin").unlink()
    original = queued.queue.call
    queued.queue.call = Hostile(original)
    (queued.root / "a.txt").write_text("a\n")
    assert await queued.queue.snapshot(PID) is None  # a ref that is not one
    (queued.root / "b.txt").write_text("b\n")
    assert await queued.queue.snapshot(PID) is None  # not a git bundle
    errors = [row.error for row in await rows(queued.db, "failed")]
    assert "not one" in errors[1] and "not a git bundle" in errors[2]
    assert await rows(queued.db, "stored") == []
    queued.queue.call = original
    (queued.root / "c.txt").write_text("c\n")
    after = await queued.queue.snapshot(PID)
    assert after is not None and after.kind == "full"  # a failure starts a new chain


def snap(sid: str, hours_ago: float, kind: str = "incr", parent: str | None = None) -> Any:
    return ProjectSnapshot(id=sid, project_id=PID, kind=kind, parent_id=parent, state="stored",
                           created_at=NOW - hours_ago * 3600)  # fmt: skip


def test_retention_keeps_whole_chains() -> None:
    keep = SnapshotSettings(keep_all_h=48, keep_daily_days=30, keep_weekly_weeks=26)
    history = [
        snap("full-old", 24 * 200, "full"),  # older than 26 weeks
        snap("full-a", 24 * 40, "full"),  # the newest of its week
        snap("a1", 24 * 40 - 1, parent="full-a"),  # newer, same week: kept, needs full-a
        snap("a2", 24 * 10, parent="a1"),
        snap("a3", 24 * 10 - 2, parent="a2"),  # the newest of its day: needs a2, a1, full-a
        snap("full-b", 30, "full"),  # within 48 hours: everything stays
        snap("b1", 5, parent="full-b"),
        snap("b2", 1, parent="b1"),
    ]
    assert kept(history, NOW, keep) == {"full-a", "a1", "a2", "a3", "full-b", "b1", "b2"}
    lonely = [snap("only", 24 * 400, "full")]
    assert kept(lonely, NOW, keep) == {"only"}  # the newest one always stays


async def test_retention_deletes_only_what_no_kept_one_needs(queued: Queued) -> None:
    made = []
    for name in ("x", "y", "z"):
        (queued.root / f"{name}.txt").write_text(f"{name}\n")
        made.append(await queued.queue.snapshot(PID))
    queued.queue.restored.add(PID)  # after a restore the next one is full
    (queued.root / "w.txt").write_text("w\n")
    newest = await queued.queue.snapshot(PID)
    assert newest is not None and newest.kind == "full"
    async with queued.db.session() as session, session.begin():
        for row, days in zip([*made, newest], (90, 89, 88, 1), strict=True):
            stored = await session.get(ProjectSnapshot, row.id if row else "")
            assert stored is not None
            stored.created_at = time.time() - days * 86400
    queued.queue.settings.snapshots.keep_daily_days = 0
    queued.queue.settings.snapshots.keep_weekly_weeks = 0
    assert await queued.queue.retain() == 3  # the old chain goes; the newest is full
    assert [row.id for row in await rows(queued.db, "stored")] == [newest.id]
    assert not queued.store.path(PID, made[0].id if made[0] else "").exists()


# The whole way: a chat run, this server's disk, a storage node, a restore. -------------------


def on_server(server: LiveServer, work: Coroutine[Any, Any, Any]) -> "asyncio.Future[Any]":
    """Run `work` on the live server's own event loop (its queues belong there)."""
    ticker = server.services.snapshots.ticker
    assert ticker is not None
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, ticker.get_loop()))


async def stored(server: LiveServer, count: int) -> list[ProjectSnapshot]:
    async with asyncio.timeout(60):
        while len(found := await rows(server.services.db, "stored")) < count:
            await asyncio.sleep(0.1)
    return found


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    monkeypatch.setattr(deploy_api, "POLL_WAIT_S", 1.0)
    script = fake_script(call("write_file", path="made.txt", content="by the agent\n"),
                         {"text": "Done."})  # fmt: skip
    settings = dev_settings(tmp_path / "data", script)
    settings.hosting.enabled, settings.snapshots.debounce_s = True, 0.2
    with LiveServer(settings) as server:
        yield server


async def test_a_run_is_kept_on_a_storage_node_and_restored_as_a_branch(
    live: LiveServer, tmp_path: Path
) -> None:
    async with httpx.AsyncClient(base_url=live.url, headers=live.headers(), timeout=60) as web:
        pid = (await web.post("/api/projects", json={"name": "Shop"})).json()["id"]
        workspace = live.services.driver.workspace(pid)
        (workspace / "notes.md").write_text("not committed\n")
        chat = (await web.post(f"/api/projects/{pid}/chats", json={})).json()["id"]
        sent = await web.post(f"/api/chats/{chat}/messages", json={"text": "Write made.txt"})
        assert sent.status_code == 202, sent.text
        first = (await stored(live, 1))[0]  # one snapshot after the run
        assert first.chat_id == chat and first.node_id is None
        listed = (await web.get(f"/api/projects/{pid}/snapshots")).json()
        assert listed["storage"] == "local" and listed["snapshots"][0]["where"] == "local"
        assert listed["snapshots"][0]["commit"] == first.commit[:7]
        node = HostStandIn()
        host, token = await create_host(live.services.db, "vault-1", node.public_key, ["storage"])
        blobs = BlobStore(tmp_path / "node-blobs", keep_free=0)
        worker = HostClient(live.url, token, node, node.key, slots=1, blobs=blobs)  # type: ignore[arg-type]
        stop = asyncio.Event()
        serving = asyncio.create_task(worker.serve(stop))
        try:
            async with asyncio.timeout(30):
                while await on_server(live, live.services.snapshots.store.node()) is None:
                    await asyncio.sleep(0.2)
            assert await on_server(live, live.services.snapshots.tick()) == (0, 1)  # moved
            kept_row = (await rows(live.services.db, "stored"))[0]
            assert kept_row.node_id == host.id
            assert not live.services.snapshots.store.path(pid, first.id).exists()
            blob = tmp_path / "node-blobs" / pid / first.id
            assert blob.is_file() and (
                sys.platform == "win32" or blob.stat().st_mode & 0o777 == 0o600
            )
            (workspace / "made.txt").write_text("changed after the snapshot\n")
            restored = await web.post(f"/api/projects/{pid}/snapshots/{first.id}/restore")
            assert restored.status_code == 200, restored.text
        finally:
            stop.set()
            await asyncio.wait_for(serving, 10)
    branch = restored.json()["branch"]
    assert (
        branch.startswith("restore/")
        and git(workspace, "symbolic-ref", "--short", "HEAD") == "main"
    )
    assert git(workspace, "rev-parse", f"{branch}^{{tree}}") == first.tree
    assert git(workspace, "show", f"{branch}:made.txt") == "by the agent"
    assert git(workspace, "show", f"{branch}:notes.md") == "not committed"
    assert (workspace / "made.txt").read_text() == "changed after the snapshot\n"  # untouched
    async with live.services.db.session() as session:
        audited = await session.scalar(
            select(AuditEntry).where(AuditEntry.action == "snapshot_restore")
        )
    assert audited is not None and audited.target == pid


async def test_only_owners_and_editors_see_and_restore_snapshots(live: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=live.url, headers=live.headers(), timeout=60) as web:
        pid = (await web.post("/api/projects", json={"name": "Mine"})).json()["id"]
    viewer, stranger = await person(live, "viewer"), await person(live, "stranger")
    async with live.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=pid, user_id=viewer.id, role="viewer"))
    sid = "0" * 32
    for who, status in ((viewer, 403), (stranger, 404)):
        assert (await who.web.get(f"/api/projects/{pid}/snapshots")).status_code == status
        restore = await who.web.post(f"/api/projects/{pid}/snapshots/{sid}/restore")
        assert restore.status_code == status
    node = HostStandIn()
    _, token = await create_host(live.services.db, "vault-2", node.public_key, ["storage"])
    async with httpx.AsyncClient(base_url=live.url, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"}) as host:  # fmt: skip
        assert (await host.get(f"/api/host/blob/{sid}")).status_code == 409  # not handed to it
        assert (await host.put(f"/api/host/blob/{sid}", content=b"x")).status_code == 409
