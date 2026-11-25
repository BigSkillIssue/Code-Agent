"""Forge Web's product factory: the files of a new product, rendered from the templates in
`products/templates/<kind>/**.tmpl` (W51; before, Forge's own `forge app new` templates).

Kinds: `fullstack` (a FastAPI server on PostgreSQL with a React web client, accounts, moderation,
payments on request), `site` (a website without a server), and the clients a fullstack product
may add: `android/`, `windows/` and `apple/`. A path part `dot-x` becomes `.x`; `@@name@@` and
`@@title@@` are the product's slug and title; a feature's own files live in `+<feature>/`, and
lines between `@@if <feature>@@` and `@@end@@` (with an optional `@@else@@`) are kept only with it.

Forge's helpers that hold no template text (the Gradle wrapper, the Windows icon, the Apple
client) are used as they are.
"""

import json
import re
from collections.abc import Collection
from importlib import resources
from importlib.resources.abc import Traversable

from forge import android_template as android
from forge import windows_template as windows
from forge.app_manifest import SLUG
from forge.apple_client import client_files

KINDS = ("fullstack", "site")
SUFFIX = ".tmpl"
NAME = re.compile(SLUG)
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,59}$")  # safe in code, JSON, TOML and HTML
PLACEHOLDER = re.compile(r"@@([a-z_]+)@@")
FEATURES = ("payments", "apple", "android", "windows", "digital_goods")
CLIENTS = ("apple", "android", "windows")
APPS_DOMAIN = "apps.example.com"  # until the server names its apps domain
OPTIONAL = "+"


class ProductError(ValueError):
    """A product cannot be made as asked; the message says why."""


def check_name(name: str, title: str) -> None:
    """Refuse a slug or title that is not safe everywhere it is written."""
    if not NAME.match(name):
        raise ProductError(f"{name!r} is not a product name: lowercase letters, digits and dashes")
    if not TITLE.match(title):
        raise ProductError(f"{title!r} is not a title: letters, digits, spaces and dashes, 1-60")


def title_of(name: str) -> str:
    """A human title from a slug: "tally-notes" -> "Tally Notes"."""
    return " ".join(part.capitalize() for part in name.split("-") if part)


def template_files(kind: str, features: Collection[str] = ()) -> dict[str, str]:
    """Every template file of a kind, with the asked features' own files, as {path: text}."""
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise ProductError(f"no such feature: {', '.join(unknown)}")
    root = resources.files("forge_sandbox.products").joinpath("templates", kind)
    files: dict[str, str] = {}
    _collect(root, "", files)
    if not files:
        raise ProductError(f"no templates for {kind!r}")
    for feature in sorted(features):
        own = root.joinpath(OPTIONAL + feature)
        if own.is_dir():
            _collect(own, "", files)
    return files


def _collect(folder: Traversable, prefix: str, files: dict[str, str]) -> None:
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(OPTIONAL):
            continue
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _collect(entry, f"{prefix}{name}/", files)
        elif name.endswith(SUFFIX):
            files[prefix + name.removesuffix(SUFFIX)] = entry.read_text(encoding="utf-8")


def encoded(path: str, text: str) -> bytes:
    """LF line ends, except the CRLF a Windows batch file needs."""
    return (text.replace("\n", "\r\n") if path.endswith(".bat") else text).encode()


def render(text: str, values: dict[str, str], features: Collection[str] = ()) -> str:
    """Feature blocks resolved, placeholders filled; an unknown one is a template bug."""
    return PLACEHOLDER.sub(lambda found: values[found.group(1)], blocks(text, features))


def blocks(text: str, features: Collection[str]) -> str:
    """The text with each `@@if feature@@ ... [@@else@@ ...] @@end@@` block resolved."""
    kept: list[str] = []
    block: list[bool] | None = None
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


def product_files(
    kind: str,
    name: str,
    title: str | None = None,
    features: Collection[str] = (),
    apps_domain: str = APPS_DOMAIN,
) -> dict[str, bytes]:
    """A new product's files as {relative path: bytes}."""
    if kind not in KINDS:
        raise ProductError(f"no such kind of product: {kind!r} (one of {', '.join(KINDS)})")
    title = title or title_of(name)
    check_name(name, title)
    if kind == "site" and features:
        raise ProductError("a website has no server, so no payments and no app clients")
    if "digital_goods" in features and "payments" not in features:
        features = [*features, "payments"]
    clients = ["web", *(c for c in CLIENTS if c in features)]
    values = {"name": name, "title": title, "clients": ", ".join(f'"{c}"' for c in clients)}
    files = {
        path: encoded(path, render(text, values, features))
        for path, text in template_files(kind, features).items()
    }
    if kind == "fullstack":
        files |= client_apps(name, title, files["server/openapi.json"], features, apps_domain)
    return files


def client_apps(
    name: str, title: str, snapshot: bytes, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The app clients asked for, each a client of the product's own server."""
    made: dict[str, bytes] = {}
    if "apple" in features:
        made |= apple_app(name, title, snapshot, features, apps_domain)
    if "android" in features:
        made |= android_app(name, title, snapshot, features, apps_domain)
    if "windows" in features:
        made |= windows_app(name, title, features, apps_domain)
    return made


def urls(name: str, apps_domain: str) -> dict[str, str]:
    """The product's staging and production addresses on Forge's servers."""
    return {"staging": f"https://{name}.staging.{apps_domain}",
            "production": f"https://{name}.{apps_domain}"}  # fmt: skip


def android_app(
    name: str, title: str, snapshot: bytes, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The Android app, in `android/`."""
    package = android.package_of(name)
    values = {"name": name, "title": title, "package": package, **urls(name, apps_domain),
              "app_tags": android.app_tags(api_tags(snapshot))}  # fmt: skip
    own = [f for f in features if f == "digital_goods"]
    made = {android.placed(path, package): encoded(path, render(text, values, own))
            for path, text in template_files(android.KIND, own).items()}  # fmt: skip
    made[android.WRAPPER] = android.wrapper_jar()
    return {f"android/{path}": data for path, data in made.items()}


def windows_app(
    name: str, title: str, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The Windows app, in `windows/`, downloaded from the product's website."""
    values = {"name": name, "title": title, "namespace": windows.namespace_of(title),
              **urls(name, apps_domain)}  # fmt: skip
    own = [f for f in features if f == "payments"]
    made = {path: encoded(path, render(text, values, own))
            for path, text in template_files(windows.KIND, own).items()}  # fmt: skip
    made[windows.ICON] = windows.icon()
    return {f"windows/{path}": data for path, data in made.items()}


def apple_app(
    name: str, title: str, snapshot: bytes, features: Collection[str], apps_domain: str
) -> dict[str, bytes]:
    """The Apple app (iPhone, iPad, Mac, Watch), in `apple/`."""
    swift = "".join(part.capitalize() for part in re.split(r"[ -]+", title) if part)[:30]
    swift = swift if swift[:1].isalpha() else f"App{swift}"[:30]
    where = urls(name, apps_domain)
    bundle = "com.example." + name.replace("-", "")
    made = client_files(swift, bundle, (where["staging"], where["production"]),
                        api_tags(snapshot), "digital_goods" in features)  # fmt: skip
    return {f"apple/{path}": data for path, data in made.items()}


def api_tags(snapshot: bytes) -> list[str]:
    """The tags of the operations in an API snapshot."""
    paths = json.loads(snapshot)["paths"].values()
    return sorted({tag for path in paths for operation in path.values()
                   for tag in operation.get("tags", [])})  # fmt: skip
