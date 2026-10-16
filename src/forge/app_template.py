"""A new full-stack product (`forge app new`, S64): a FastAPI server on PostgreSQL with accounts,
moderation and migrations, its docs and CI, and the manifest hosting reads.

The files live as package data in `templates/<kind>/**.tmpl`, so Forge's own lint and type
checks never see the product's code. A path part `dot-x` becomes `.x`; `@@name@@` and
`@@title@@` in a file are the product's slug and its title.

Optional parts (S69): the files of a feature, such as `payments`, live in
`templates/<kind>/+<feature>/` and are added to the product, or replace the file of the same
path, only when it is asked for; lines between `@@if <feature>@@` and `@@end@@` (with an
optional `@@else@@`) are kept only with it, the lines after `@@else@@` only without it.
"""

import json
import re
from collections.abc import Collection
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from forge import android_template as android
from forge import windows_template as windows
from forge.app_manifest import SLUG
from forge.apple_client import client_files

KIND = "fullstack"
SUFFIX = ".tmpl"
NAME = re.compile(SLUG)
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,59}$")  # safe in code, JSON, TOML and HTML
PLACEHOLDER = re.compile(r"@@([a-z_]+)@@")
FEATURES = ("payments", "apple", "android", "windows", "digital_goods")  # `forge app new --...`
APPS_DOMAIN = "apps.example.com"  # Forge Web's domain for hosted apps, until one is given
OPTIONAL = "+"  # the folder of a feature's own files: templates/<kind>/+<feature>/


def name_problem(name: str) -> str | None:
    """Why a product name cannot be used, or None."""
    if NAME.match(name):
        return None
    return f"{name!r} is not a product name: lowercase letters, digits and dashes, 1-40 long"


def title_problem(title: str) -> str | None:
    """Why a product title cannot be used, or None."""
    if TITLE.match(title):
        return None
    return f"{title!r} is not a title: letters, digits, spaces and dashes, 1-60 long"


def title_of(name: str) -> str:
    """A human title from a slug: "tally-notes" → "Tally Notes"."""
    return " ".join(part.capitalize() for part in name.split("-") if part)


def template_files(kind: str = KIND, features: Collection[str] = ()) -> dict[str, str]:
    """Every template file of a kind, with the files of the features asked for, as {path in
    the product: text with placeholders}."""
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise ValueError(f"no such feature: {', '.join(unknown)}")
    root = resources.files("forge").joinpath("templates", kind)
    files: dict[str, str] = {}
    _collect(root, "", files)
    if not files:
        raise FileNotFoundError(f"no templates for {kind!r} in the forge package")
    for feature in sorted(features):
        own = root.joinpath(OPTIONAL + feature)
        if own.is_dir():  # a feature may need no files of its own, only blocks
            _collect(own, "", files)
    return files


def _collect(folder: Traversable, prefix: str, files: dict[str, str]) -> None:
    """Add the template files below a folder to `files`."""
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(OPTIONAL):
            continue  # a feature's files: added by template_files when asked for
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _collect(entry, f"{prefix}{name}/", files)
        elif name.endswith(SUFFIX):
            files[prefix + name.removesuffix(SUFFIX)] = entry.read_text(encoding="utf-8")


def encoded(path: str, text: str) -> bytes:
    """A rendered file's bytes: LF line ends, except the CRLF a Windows batch file needs (a
    template is read with LF whatever line ends the checkout gave it)."""
    return (text.replace("\n", "\r\n") if path.endswith(".bat") else text).encode()


def render(text: str, values: dict[str, str], features: Collection[str] = ()) -> str:
    """The text with its feature blocks resolved and its placeholders filled; an unknown
    placeholder or feature is a template bug."""
    return PLACEHOLDER.sub(lambda found: values[found.group(1)], blocks(text, features))


def blocks(text: str, features: Collection[str]) -> str:
    """The text with each `@@if feature@@ ... [@@else@@ ...] @@end@@` block resolved."""
    kept: list[str] = []
    block: list[bool] | None = None  # [the feature is asked for, in its @@else@@ part]
    for line in text.splitlines(keepends=True):
        marker = line.strip()
        if marker.startswith("@@if ") and marker.endswith("@@"):
            feature = marker[5:-2]
            if block is not None or feature not in FEATURES:
                raise ValueError(f"a nested block or an unknown feature: {marker}")
            block = [feature in features, False]
        elif marker == "@@else@@" and block is not None:
            block[1] = True
        elif marker == "@@end@@" and block is not None:
            block = None
        elif block is None or block[0] != block[1]:
            kept.append(line)
    if block is not None:
        raise ValueError("a block is not closed with @@end@@")
    return "".join(kept)


def app_files(
    name: str,
    title: str | None = None,
    kind: str = KIND,
    features: Collection[str] = (),
    apps_domain: str = APPS_DOMAIN,
) -> dict[str, bytes]:
    """The product's files as {relative path: bytes}."""
    clients = ["web", *(c for c in ("apple", "android", "windows") if c in features)]
    values = {"name": name, "title": title or title_of(name),
              "clients": ", ".join(f'"{c}"' for c in clients)}  # fmt: skip
    files = {
        path: encoded(path, render(text, values, features))
        for path, text in template_files(kind, features).items()
    }
    if "apple" in features:
        files |= apple_app(name, values["title"], files["server/openapi.json"], features,
                           apps_domain)  # fmt: skip
    if "android" in features:
        files |= android_app(name, values["title"], files["server/openapi.json"], features,
                             apps_domain)  # fmt: skip
    if "windows" in features:
        files |= windows_app(name, values["title"], features, apps_domain)
    return files


def android_app(
    name: str, title: str, snapshot: bytes, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The product's Android app (S71), in `android/`: a client of its server's API."""
    package = android.package_of(name)
    values = {"name": name, "title": title, "package": package,
              "staging": f"https://{name}.staging.{apps_domain}",
              "production": f"https://{name}.{apps_domain}",
              "app_tags": android.app_tags(api_tags(snapshot))}  # fmt: skip
    own = [f for f in features if f == "digital_goods"]
    made = {android.placed(path, package): encoded(path, render(text, values, own))
            for path, text in template_files(android.KIND, own).items()}  # fmt: skip
    made[android.WRAPPER] = android.wrapper_jar()
    return {f"android/{path}": data for path, data in made.items()}


def windows_app(
    name: str, title: str, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The product's Windows app (S72), in `windows/`: a client of its server's API."""
    values = {"name": name, "title": title, "namespace": windows.namespace_of(title),
              "staging": f"https://{name}.staging.{apps_domain}",
              "production": f"https://{name}.{apps_domain}"}  # fmt: skip
    own = [f for f in features if f == "digital_goods"]
    made = {path: encoded(path, render(text, values, own))
            for path, text in template_files(windows.KIND, own).items()}  # fmt: skip
    made |= {f"App/{path}": data for path, data in windows.logos().items()}
    return {f"windows/{path}": data for path, data in made.items()}


def api_tags(snapshot: bytes) -> list[str]:
    """The tags of the operations in an API snapshot."""
    paths = json.loads(snapshot)["paths"].values()
    return sorted({tag for path in paths for operation in path.values()
                   for tag in operation.get("tags", [])})  # fmt: skip


def apple_app(
    name: str, title: str, snapshot: bytes, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The product's Apple app (S70), in `apple/`: a client of its server's API."""
    swift = "".join(part.capitalize() for part in re.split(r"[ -]+", title) if part)[:30]
    swift = swift if swift[:1].isalpha() else f"App{swift}"[:30]
    tags = api_tags(snapshot)
    urls = (f"https://{name}.staging.{apps_domain}", f"https://{name}.{apps_domain}")
    bundle = "com.example." + name.replace("-", "")
    made = client_files(swift, bundle, urls, tags, "digital_goods" in features)
    return {f"apple/{path}": data for path, data in made.items()}


def write_app(
    folder: Path,
    name: str,
    title: str | None = None,
    features: Collection[str] = (),
    apps_domain: str = APPS_DOMAIN,
) -> list[Path]:
    """Write the product into `folder`, which must be missing or empty; return the files."""
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"{folder} already exists and is not empty")
    written = []
    for relative, data in app_files(
        name, title, features=features, apps_domain=apps_domain
    ).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if data.startswith(b"#!"):  # a script, such as the Gradle wrapper's
            path.chmod(path.stat().st_mode | 0o111)
        written.append(path)
    return written
