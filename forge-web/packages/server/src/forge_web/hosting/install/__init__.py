"""The node installer Forge Web hands out (W64): one command makes a Linux server a node.

`node.sh.tmpl` is a bash script with two placeholders, Forge Web's address and the default apps
domain. Both are checked against strict patterns first and then shell-quoted, so nothing a
setting holds can become a command on the node: the script runs as root there.
"""

import re
import shlex
from importlib.resources import files

TEMPLATE = "node.sh.tmpl"
ROLES = ("apps", "db", "ai", "storage")
# A plain https address: a host name, an optional port and an optional path, nothing else.
HTTPS_URL = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?(?::[0-9]{1,5})?"
                       r"(?:/[A-Za-z0-9._~/-]*)?")  # fmt: skip
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+")
JOIN_CODE = re.compile(r"fjn_[A-Za-z0-9_-]{1,200}")


def https_base(url: str) -> str:
    """`url` without a trailing slash, if it is a plain https address; ValueError otherwise."""
    base = url.rstrip("/")
    if not HTTPS_URL.fullmatch(base):
        raise ValueError(f"not a plain https:// address: {url!r}")
    return base


def node_script() -> str:
    """The node installer's template, as it ships in the package."""
    return files(__name__).joinpath(TEMPLATE).read_text(encoding="utf-8")


def render_node_script(base_url: str, apps_domain: str = "") -> str:
    """The node installer for a Forge Web at `base_url` (apps at `apps_domain` by default)."""
    base = https_base(base_url)
    if apps_domain and not DOMAIN.fullmatch(apps_domain):
        raise ValueError(f"not a domain: {apps_domain!r}")
    return (
        node_script()
        .replace("@@FORGE_SERVER@@", shlex.quote(base))
        .replace("@@APPS_DOMAIN@@", shlex.quote(apps_domain))
    )


def install_command(base_url: str, code: str, roles: list[str]) -> str:
    """The one line the admin runs on a new node, for this join code and these roles."""
    base = https_base(base_url)
    if not JOIN_CODE.fullmatch(code):
        raise ValueError("not a join code")
    if not roles or any(role not in ROLES for role in roles):
        raise ValueError(f"roles are some of {', '.join(ROLES)}")
    script = shlex.quote(f"{base}/install/node.sh")
    return (
        f"curl -fsSL {script} | sudo bash -s -- --code {shlex.quote(code)}"
        f" --roles {shlex.quote(','.join(roles))}"
    )
