"""A git job: one push or fetch that needs the network and the user's token.

It never runs in the project's sandbox and never runs git inside the project's repository:
commits travel as bundle files in `.git/forge-transfer/`. The job unpacks a bundle into a
fresh repository in its own temporary folder (or packs one from it), with no system or global
git config, hooks switched off, https only, no redirects, and the token in a credential file in
that temporary folder, read from stdin. Docker runs it in a throwaway container; local mode
(development) in a child process.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Literal

TRANSFER = ".git/forge-transfer"
SCRIPT = r"""
set -eu
umask 077
mkdir -p "$HOME"
cat > "$JOB_TMP/credentials"
transfer="$WORKSPACE/.git/forge-transfer"
safe() {
  git -c core.hooksPath=/dev/null -c core.fsmonitor= -c credential.helper= \
    -c "credential.helper=store --file=$JOB_TMP/credentials" -c http.followRedirects=false \
    -c protocol.allow=never -c protocol.https.allow=always $GIT_EXTRA "$@"
}
git init -q --bare "$JOB_TMP/repo"
case "$ACTION" in
  push)
    safe -c protocol.file.allow=always -C "$JOB_TMP/repo" fetch -q --no-tags \
      "$transfer/push.bundle" "+refs/heads/$BRANCH:refs/heads/$BRANCH"
    rm -f "$transfer/push.bundle"
    safe -C "$JOB_TMP/repo" push --porcelain "$URL" "refs/heads/$BRANCH:refs/heads/$REMOTE_BRANCH"
    ;;
  fetch)
    safe -C "$JOB_TMP/repo" fetch --no-tags "$URL" "+refs/heads/$REMOTE_BRANCH:refs/heads/$BRANCH"
    mkdir -p "$transfer"
    rm -f "$transfer/fetch.bundle"
    git -C "$JOB_TMP/repo" bundle create -q "$transfer/fetch.bundle" "refs/heads/$BRANCH"
    ;;
  *)
    echo "unknown action" >&2
    exit 2
    ;;
esac
"""


@dataclass
class GitJob:
    """What one job does."""

    action: Literal["push", "fetch"]
    url: str
    branch: str  # the project's branch
    remote_branch: str  # the branch at the remote
    credentials: str = ""  # one git-credential-store line, or nothing
    pin: tuple[str, str] | None = None  # the URL's host and the checked address it must use
    local_remotes: bool = False  # development and tests: file:// remotes
    timeout: float = 300
    extra_env: dict[str, str] = field(default_factory=dict)  # e.g. the server's HTTPS proxy

    def env(self, workspace: str, tmp: str) -> dict[str, str]:
        """The job's environment (never the token: that comes on stdin)."""
        return {
            **self.extra_env,
            "ACTION": self.action,
            "URL": self.url,
            "BRANCH": self.branch,
            "REMOTE_BRANCH": self.remote_branch,
            "WORKSPACE": workspace,
            "JOB_TMP": tmp,
            "HOME": f"{tmp}/home",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EXTRA": "-c protocol.file.allow=always" if self.local_remotes else "",
            "LC_ALL": "C",
        }

    def stdin(self) -> bytes:
        """What the job reads first: the credential line."""
        return (self.credentials + "\n").encode() if self.credentials else b""


MAX_OUTPUT = 20_000


async def run_job(
    argv: list[str], env: dict[str, str] | None, stdin: bytes, timeout: float
) -> tuple[int, str]:
    """Run a job's process to the end: exit code and the end of its output (-1 on timeout)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"the git job took longer than {timeout:.0f} s"
    code = proc.returncode if proc.returncode is not None else -1
    return code, out.decode("utf-8", "replace")[-MAX_OUTPUT:]
