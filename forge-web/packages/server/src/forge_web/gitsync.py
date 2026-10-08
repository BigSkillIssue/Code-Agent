"""Pushing and pulling a project: the project packs or unpacks bundles (no network, no token),
a git job outside the project moves them (network and the user's token, nothing else).

The remote must be an https URL whose host is allowed (`git.hosts`) and resolves to public
addresses only; the job connects to exactly the address that was checked.
"""

import asyncio
import os
import re
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import HTTPException

from forge_web.audit import audit
from forge_web.auth.git_credentials import credential_for
from forge_web.containers.gitjob import GitJob
from forge_web.db.models import User
from forge_web.egress import Denied, EgressPolicy, resolve
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
BRANCH = re.compile(r"^(?![-/.])(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![/.])$")
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")


def remote_problem(url: str) -> str | None:
    """Why a remote URL is not accepted (https only, no credentials in it), or None."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname:
        return "the remote must be an https:// URL"
    if parts.username or parts.password:
        return "the URL must not contain a user name or token; connect the host in your settings"
    if CONTROL.search(url) or " " in url.strip():
        return "the URL contains spaces or control characters"
    return None


def local_remotes(services: Services) -> bool:
    """file:// remotes and private addresses (development and tests only)."""
    return services.settings.git.allow_local_remotes and services.settings.dev.enabled


def check_branch(name: str) -> str:
    """A branch name safe to pass to the job; 422 otherwise."""
    if not BRANCH.match(name):
        raise HTTPException(422, f"{name!r} is not a valid branch name")
    return name


def store_line(host: str, credential: tuple[str, str] | None) -> str:
    """A git-credential-store line for the host, or nothing."""
    if credential is None or not host:
        return ""
    user, token = credential
    return f"https://{quote(user or 'x-access-token', safe='')}:{quote(token, safe='')}@{host}"


def mask(text: str, credential: tuple[str, str] | None) -> str:
    """The job's output without the token (in plain or URL-encoded form)."""
    if credential is not None and credential[1]:
        for form in {credential[1], quote(credential[1], safe="")}:
            text = text.replace(form, "***")
    return text


async def remote_target(services: Services, project_id: str) -> tuple[str, str, str | None]:
    """The project's remote URL, its host and the checked address to use (None for file://)."""
    found = result_dict(await sandbox_call(services, project_id, "git.remote"))
    url = found.get("url")
    if not isinstance(url, str) or not url:
        raise HTTPException(409, "the project has no remote yet; set one first")
    if local_remotes(services) and url.startswith("file://"):
        return url, "", None
    problem = remote_problem(url)
    if problem:
        raise HTTPException(422, problem)
    host = urlsplit(url).hostname or ""
    policy = EgressPolicy(services.settings.git.hosts, allow_private=local_remotes(services))
    if not policy.host_allowed(host):
        raise HTTPException(403, f"this server does not push to or pull from {host}")
    try:
        address = await resolve(host, urlsplit(url).port or 443, policy)
    except Denied as err:
        raise HTTPException(403, str(err)) from None
    return url, host, address


async def current_branch(services: Services, project_id: str) -> str:
    """The checked-out branch; 409 if none (a detached HEAD)."""
    found = result_dict(await sandbox_call(services, project_id, "git.branches"))
    current = found.get("current")
    if not isinstance(current, str) or not current:
        raise HTTPException(409, "no branch is checked out")
    return current


async def run(
    services: Services,
    project_id: str,
    user: User,
    action: str,
    branches: tuple[str, str],
    target: tuple[str, str, str | None],
) -> str:
    """Run one job (push or fetch) for the user to the checked remote `target`; its output."""
    url, host, address = target
    branch, remote_branch = branches
    credential = await credential_for(services, user.id, host) if host else None
    job = GitJob(
        "push" if action == "push" else "fetch", url, branch, remote_branch,
        credentials=store_line(host, credential),
        pin=(host, address) if host and address else None,
        local_remotes=local_remotes(services), timeout=services.settings.git.timeout_s,
        extra_env={k: os.environ[k] for k in PROXY_ENV if k in os.environ},
    )  # fmt: skip
    code, output = await services.driver.run_git_job(project_id, job)
    output = mask(output, credential)
    if code != 0:
        last = output.strip().splitlines()[-3:]
        raise HTTPException(502, f"git {job.action} failed: " + " / ".join(last)[:1000])
    return output


def project_lock(services: Services, project_id: str) -> asyncio.Lock:
    """The lock that keeps a project's git jobs one at a time."""
    return services.git_locks.setdefault(project_id, asyncio.Lock())


async def push(
    services: Services, project_id: str, user: User, branch: str, remote_branch: str, ip: str
) -> dict[str, Any]:
    """Push a branch of the project to its remote."""
    async with project_lock(services, project_id):
        target = await remote_target(services, project_id)
        packed = result_dict(await sandbox_call(services, project_id, "git.bundle_out",
                                                {"branch": branch}))  # fmt: skip
        output = await run(services, project_id, user, "push", (branch, remote_branch), target)
    await audit(services.db, "git_push", user_id=user.id, target=project_id, ip=ip, branch=branch)
    return {"ok": True, "head": packed.get("head"), "output": output}


async def pull(
    services: Services, project_id: str, user: User, branch: str, remote_branch: str, ip: str
) -> dict[str, Any]:
    """Fetch a branch from the remote and fast-forward the project's branch to it."""
    async with project_lock(services, project_id):
        target = await remote_target(services, project_id)
        output = await run(services, project_id, user, "fetch", (branch, remote_branch), target)
        merged = result_dict(await sandbox_call(services, project_id, "git.bundle_in",
                                                {"branch": branch}))  # fmt: skip
    await audit(services.db, "git_pull", user_id=user.id, target=project_id, ip=ip, branch=branch)
    return {"ok": True, **merged, "output": output}
