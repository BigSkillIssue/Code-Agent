"""Backups that leave the host (W29a): the app's database (`pg_dump -Fc`) and its files, packed
and encrypted on the host before they go to Forge Web, and the restore drill that proves a
backup can be read back.

The key is the app's own (32 bytes), made and kept by Forge Web and sealed to this host for each
job. The file is encrypted in chunks (ChaCha20-Poly1305, the "STREAM" construction): a header
with a random nonce prefix, then chunks whose nonce counts up and whose last one is marked, so a
cut-off or reordered file does not open.
"""

import asyncio
import base64
import binascii
import os
import secrets
import shutil
import struct
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from forge_hostworker.docker import Docker, HostError
from forge_hostworker.postgres import container, throwaway_database, wait_ready
from forge_hostworker.storage import files_volume
from forge_hostworker.wire import CheckState, HostJob, JobResult

if TYPE_CHECKING:  # the runner imports this module
    from forge_hostworker.runner import HostRunner

KEY_SECRET = "BACKUP_KEY"  # the app's backup key (base64), sealed to the host like a secret
MAGIC = b"FBK1"
CHUNK = 1024 * 1024
TAG = 16
DUMP_TIMEOUT_S = 3600


class BackupError(Exception):
    """A backup file does not open (wrong key, changed or cut off)."""


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">I", counter)


def encrypt_file(key: bytes, source: Path, target: Path) -> None:
    """Encrypt a file in chunks; only the key opens it again."""
    box, prefix = ChaCha20Poly1305(key), os.urandom(8)
    size = source.stat().st_size
    with source.open("rb") as plain, target.open("wb") as out:
        out.write(MAGIC + prefix)
        counter, done = 0, 0
        while True:
            chunk = plain.read(CHUNK)
            done += len(chunk)
            last = done >= size
            out.write(box.encrypt(_nonce(prefix, counter), chunk, b"last" if last else b"more"))
            counter += 1
            if last:
                return


def decrypt_chunks(key: bytes, source: Path) -> Iterator[bytes]:
    """The plain chunks of an encrypted file; raises BackupError when it does not open."""
    box = ChaCha20Poly1305(key)
    with source.open("rb") as sealed:
        head = sealed.read(len(MAGIC) + 8)
        if head[: len(MAGIC)] != MAGIC:
            raise BackupError("not a Forge backup")
        prefix, counter, ended = head[len(MAGIC) :], 0, False
        while chunk := sealed.read(CHUNK + TAG):
            if ended:
                raise BackupError("there is more after the last chunk")
            try:
                plain = box.decrypt(_nonce(prefix, counter), chunk, b"more")
            except Exception:
                try:
                    plain = box.decrypt(_nonce(prefix, counter), chunk, b"last")
                except Exception as err:
                    raise BackupError("the backup does not open (wrong key, or changed)") from err
                ended = True
            counter += 1
            yield plain
        if not ended:
            raise BackupError("the backup is cut off")


def decrypt_file(key: bytes, source: Path, target: Path) -> None:
    """Decrypt a whole backup file."""
    with target.open("wb") as out:
        for chunk in decrypt_chunks(key, source):
            out.write(chunk)


async def make_archive(docker: Docker, app: str, environment: str, folder: Path, work: Path,
                       image: str, key: bytes) -> Path:  # fmt: skip
    """The app's database and files packed and encrypted in `work`; the encrypted file."""
    work.mkdir(parents=True, exist_ok=True)
    name = container(app, environment)
    state = await docker("container", "inspect", "-f", "{{.State.Running}}", name)
    if state.code != 0:
        raise HostError("the app has no database on this host", hint="nothing to back up")
    await wait_ready(docker, name)
    dump, files = work / "db.dump", work / "files.tar"
    dumped = await docker("exec", name, "pg_dump", "-U", "app", "-d", "app", "-Fc",
                          timeout=DUMP_TIMEOUT_S, out=dump)  # fmt: skip
    if dumped.code != 0:
        raise HostError(f"pg_dump failed: {dumped.tail()[-500:]}", hint=f"see `docker logs {name}`")
    await pack_files(docker, app, environment, image, files)
    packed = work / "backup.tar"
    with tarfile.open(packed, "w") as tar:
        tar.add(dump, "db.dump")
        tar.add(files, "files.tar")
    sealed = work / "backup.fbk"
    encrypt_file(key, packed, sealed)
    for plain in (dump, files, packed):
        plain.unlink(missing_ok=True)  # nothing readable stays behind
    return sealed


async def pack_files(docker: Docker, app: str, environment: str, image: str, target: Path) -> None:
    """The app's files volume as a tar (an empty tar when the app keeps no files)."""
    volume = files_volume(app, environment)
    if (await docker("volume", "inspect", volume)).code != 0:
        with tarfile.open(target, "w"):
            return
    packed = await docker(
        "run", "--rm", "--runtime=runsc", "--cap-drop", "ALL", "--network", "none",
        "--security-opt", "no-new-privileges", "-v", f"{volume}:/data:ro", image,
        "tar", "-C", "/data", "-cf", "-", ".", timeout=DUMP_TIMEOUT_S, out=target,
    )  # fmt: skip
    if packed.code != 0:
        raise HostError(f"the app's files could not be packed: {packed.tail()[-300:]}",
                        hint="see the Docker daemon's log")  # fmt: skip


async def restore_drill(docker: Docker, app: str, source: Path, work: Path,
                        key: bytes) -> JobResult:  # fmt: skip
    """Open the backup and restore its database into a throwaway PostgreSQL: it can be read."""
    token = secrets.token_hex(6)
    network, database = f"forge-drill-{token}", f"forge-drill-{token}-db"
    work.mkdir(parents=True, exist_ok=True)
    try:
        plain = work / "backup.tar"
        try:
            decrypt_file(key, source, plain)
        except BackupError as err:
            return JobResult(ok=False, error=str(err))
        with tarfile.open(plain) as tar:
            names = set(tar.getnames())
            if "db.dump" not in names:
                return JobResult(ok=False, error="the backup holds no database")
            tar.extract("db.dump", work, filter="data")
            kept = count_files(tar) if "files.tar" in names else 0
        return await restored(docker, app, work, network, database, kept)
    finally:
        await docker("rm", "-f", database)
        await docker("network", "rm", network)
        shutil.rmtree(work, ignore_errors=True)


def count_files(tar: tarfile.TarFile) -> int:
    """How many entries the backup's files archive holds (read as a stream)."""
    files = tar.extractfile("files.tar")
    if files is None:
        return 0
    with tarfile.open(fileobj=files, mode="r|") as inner:
        return sum(1 for _ in inner)


async def restored(docker: Docker, app: str, work: Path, network: str, database: str,
                   kept: int) -> JobResult:  # fmt: skip
    """pg_restore into the throwaway database, then count what came back."""
    await docker("network", "create", "--internal", "--label", f"forge.app={app}", network)
    await throwaway_database(docker, database, network, app, work,
                             mounts=(f"{work / 'db.dump'}:/restore/db.dump:ro",))  # fmt: skip
    done = await docker("exec", database, "pg_restore", "--no-owner", "-U", "app", "-d", "app",
                        "/restore/db.dump", timeout=DUMP_TIMEOUT_S)  # fmt: skip
    if done.code != 0:
        return JobResult(ok=False, error="pg_restore failed", log_tail=done.tail())
    counted = await docker("exec", database, "psql", "-U", "app", "-d", "app", "-tAc",
                           "select count(*) from information_schema.tables "
                           "where table_schema = 'public'")  # fmt: skip
    tables = counted.out.strip() or "0"
    check = CheckState(name="restore", ok=counted.code == 0,
                       detail=f"{tables} tables, {kept} entries of files")  # fmt: skip
    return JobResult(ok=check.ok, checks=[check], log_tail=done.tail())


def outgoing(data_dir: Path, job_id: str) -> Path:
    """Where an archive job leaves its encrypted backup for the client to upload."""
    return data_dir / "outgoing" / f"{job_id}.fbk"


def backup_key(values: dict[str, str]) -> bytes:
    """The app's backup key from the job's sealed values."""
    try:
        key = base64.b64decode(values.get(KEY_SECRET, ""), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise HostError("the job came without the app's backup key", hint="ask Forge Web again")
    return key


async def archive_job(runner: "HostRunner", job: HostJob, values: dict[str, str]) -> JobResult:
    """The app's backup, encrypted and left in `outgoing` for the client to upload."""
    live = runner.state(job.app, job.environment).live
    plan = runner.saved_plan(job.app, job.environment, live)
    if plan is None:
        return JobResult(ok=False, error="the app has no live release on this host")
    work = runner.data_dir / "archives" / job.id
    try:
        sealed = await make_archive(runner.docker, job.app, job.environment,
                                    runner.folder(job.app, job.environment), work,
                                    plan.services[0].image, backup_key(values))  # fmt: skip
        target = outgoing(runner.data_dir, job.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(sealed.replace, target)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    packed = CheckState(name="archive", ok=True, detail=f"{target.stat().st_size} bytes")
    return JobResult(ok=True, release=live or 0, checks=[packed])


async def drill_job(
    runner: "HostRunner", job: HostJob, source: Path, values: dict[str, str]
) -> JobResult:
    """Restore the backup (the job's source) into a throwaway database."""
    work = runner.data_dir / "drills" / job.id
    return await restore_drill(runner.docker, job.app, source, work, backup_key(values))
