"""Putting a product into a project folder: a new one, or more features for an existing one.

The agent calls these through its product tools (W54). Nothing the agent or the user changed is
ever overwritten: a file the template would change, but which differs from what the template
made, comes back as a diff for the agent to merge by hand.
"""

import difflib
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from forge.app_manifest import load_manifest

from forge_sandbox.products.render import CLIENTS, FEATURES, ProductError, product_files

MAX_DIFF = 20_000  # characters of one file's diff handed back


@dataclass
class Placed:
    """What placing a product did."""

    written: list[str] = field(default_factory=list)
    same: list[str] = field(default_factory=list)  # already there, byte for byte
    to_merge: dict[str, str] = field(default_factory=dict)  # path -> unified diff to apply


def write_file(root: Path, relative: str, data: bytes) -> None:
    """Write one file below the root (never outside it), keeping scripts executable."""
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ProductError(f"{relative} is outside the project")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if data.startswith(b"#!"):
        path.chmod(path.stat().st_mode | 0o111)


def diff(path: str, before: bytes, after: bytes) -> str:
    """A unified diff from what the file says now to what the template would make."""
    lines = difflib.unified_diff(
        before.decode(errors="replace").splitlines(keepends=True),
        after.decode(errors="replace").splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )  # fmt: skip
    return "".join(lines)[:MAX_DIFF]


def create(root: Path, kind: str, name: str, title: str | None, features: Collection[str],
           apps_domain: str) -> Placed:  # fmt: skip
    """A new product in the project; files that already exist with other content are left
    alone and handed back as diffs."""
    if (root / "forge.app.toml").exists():
        raise ProductError("this project already is a product: add features to it instead")
    files = product_files(kind, name, title, features, apps_domain)
    return place(root, files, {})


def add(root: Path, title: str | None, more: Collection[str], apps_domain: str) -> Placed:
    """Add app clients or features (payments, digital goods) to the product in the project."""
    manifest = load_manifest(root / "forge.app.toml")
    if isinstance(manifest, list):
        raise ProductError("forge.app.toml has problems: " + "; ".join(p.message for p in manifest))
    have: list[str] = [c for c in manifest.clients if c in CLIENTS]
    if manifest.payments.kind == "relay":
        have.append("payments")
    if manifest.payments.digital_goods:
        have.append("digital_goods")
    unknown = sorted(set(more) - set(FEATURES))
    if unknown:
        raise ProductError(f"no such feature: {', '.join(unknown)}")
    kind = "fullstack" if any(s.runtime != "static" for s in manifest.services) else "site"
    before = product_files(kind, manifest.name, title, have, apps_domain)
    after = product_files(kind, manifest.name, title, sorted({*have, *more}), apps_domain)
    return place(root, after, before)


def place(root: Path, files: dict[str, bytes], before: dict[str, bytes]) -> Placed:
    """Write what is new; replace what the template made and nobody changed; hand back the rest."""
    done = Placed()
    for relative, data in sorted(files.items()):
        path = root / relative
        current = path.read_bytes() if path.is_file() else None
        if current == data:
            done.same.append(relative)
        elif current is None or current == before.get(relative):
            write_file(root, relative, data)
            done.written.append(relative)
        else:
            done.to_merge[relative] = diff(relative, current, data)
    return done
