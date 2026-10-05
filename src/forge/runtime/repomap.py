"""Repository map: definitions per file (tree-sitter tags queries), ranked by cross-file use.

Grammars come from `tree-sitter-language-pack`, which downloads a language the first time
it is needed; a language that cannot be loaded counts as unsupported. Parsed files are cached
in `.forge/cache/repomap.json`, keyed by path and mtime.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFINITION_KINDS = ("class", "function", "method", "interface", "module", "type", "constant")
SIGNATURE_CHARS = 120
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CONSTANT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
CONSTANT_STATEMENTS = ("assignment", "expression_statement", "const", "var", "lexical_declaration")
CACHE_FILE = Path(".forge") / "cache" / "repomap.json"


@dataclass
class Definition:
    """One definition: its name, signature line and nesting depth."""

    name: str
    signature: str
    depth: int


@dataclass
class FileInfo:
    """What the map knows about one parsed file."""

    rel: str
    definitions: list[Definition]
    words: Counter[str]
    score: int = 0


@dataclass
class RepoMap:
    """The ranked files plus counts for the footer."""

    files: list[FileInfo]
    unsupported: int
    parsed: int  # files parsed this time (not from the cache)


def language_of(path: Path) -> str | None:
    """The tree-sitter language for a file, or None when it has no tags query."""
    import tree_sitter_language_pack as pack

    try:
        name = pack.detect_language_from_path(str(path))
        return name if name and pack.get_tags_query(name) else None
    except Exception:  # unknown or undownloadable languages are simply not mapped
        return None


def parse_file(path: Path, language: str) -> list[Definition]:
    """Definitions in one file, in source order."""
    import tree_sitter as ts
    import tree_sitter_language_pack as pack

    source = path.read_bytes()
    tree = pack.get_parser(language).parse(source)
    query = ts.Query(pack.get_language(language), pack.get_tags_query(language) or "")
    found: dict[tuple[int, int], tuple[str, Any]] = {}
    for _, captures in ts.QueryCursor(query).matches(tree.root_node):
        names = captures.get("name") or []
        for capture, nodes in captures.items():
            kind = capture.removeprefix("definition.")
            if capture.startswith("definition.") and kind in DEFINITION_KINDS and names:
                name = (names[0].text or b"").decode("utf-8", "replace")
                if kind != "constant" or name.isupper():
                    found[(nodes[0].start_byte, nodes[0].end_byte)] = (name, nodes[0])
    for name, node in top_level_constants(tree.root_node):
        found.setdefault((node.start_byte, node.end_byte), (name, node))
    ordered = sorted(found.values(), key=lambda v: v[1].start_byte)
    return [definition(name, node, found) for name, node in ordered]


def top_level_constants(root: Any) -> list[tuple[str, Any]]:
    """UPPER_CASE names assigned by top-level statements (tags queries rarely include them)."""
    constants: list[tuple[str, Any]] = []
    for statement in root.named_children:
        if not any(word in statement.type for word in CONSTANT_STATEMENTS):
            continue
        name = first_identifier(statement, depth=3)
        if name and CONSTANT_NAME.fullmatch(name):
            constants.append((name, statement))
    return constants


def first_identifier(node: Any, depth: int) -> str | None:
    """The first identifier within `depth` levels below `node` (breadth first)."""
    level = list(node.named_children)
    for _ in range(depth):
        for child in level:
            if child.type in ("identifier", "constant", "type_identifier"):
                return str((child.text or b"").decode("utf-8", "replace"))
        level = [grandchild for child in level for grandchild in child.named_children]
    return None


def definition(
    name: str, node: Any, all_found: dict[tuple[int, int], tuple[str, Any]]
) -> Definition:
    """A Definition with its first line as signature and depth = enclosing definitions."""
    first = (node.text or b"").decode("utf-8", "replace").splitlines()[0].strip()
    signature = first.rstrip("{:").rstrip()[:SIGNATURE_CHARS]
    depth = sum(1 for start, end in all_found if start < node.start_byte and node.end_byte <= end)
    return Definition(name=name, signature=signature, depth=depth)


def build_map(root: Path, files: list[Path]) -> RepoMap:
    """Parse (or load from cache) every file and rank them. Blocking: run in a thread."""
    cache = load_cache(root)
    infos: list[FileInfo] = []
    unsupported = parsed = 0
    for path in files:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        mtime = path.stat().st_mtime_ns
        entry = cache.get(rel)
        if entry is None or entry.get("mtime") != mtime:
            entry = parse_entry(path, mtime)
            parsed += 1 if entry["language"] is not None else 0
            cache[rel] = entry
        if entry["language"] is None:
            unsupported += 1
            continue
        defs = [Definition(*d) for d in entry["definitions"]]
        infos.append(FileInfo(rel=rel, definitions=defs, words=Counter(entry["words"])))
    save_cache(root, cache)
    rank(infos)
    return RepoMap(files=infos, unsupported=unsupported, parsed=parsed)


def parse_entry(path: Path, mtime: int) -> dict[str, Any]:
    """A cache entry for one file; language None marks unsupported files."""
    language = language_of(path)
    if language is None:
        return {"mtime": mtime, "language": None}
    try:
        defs = parse_file(path, language)
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # a file that fails to parse is treated like an unsupported one
        return {"mtime": mtime, "language": None}
    return {
        "mtime": mtime,
        "language": language,
        "definitions": [[d.name, d.signature, d.depth] for d in defs],
        "words": dict(Counter(WORD.findall(text))),
    }


def rank(infos: list[FileInfo]) -> None:
    """Score = uses of the file's names in other files, plus 1; sort by score, then path."""
    total: Counter[str] = Counter()
    for info in infos:
        total.update(info.words)
    for info in infos:
        names = {d.name for d in info.definitions}
        info.score = 1 + sum(total[n] - info.words.get(n, 0) for n in names)
    infos.sort(key=lambda i: (-i.score, i.rel))


def render_map(repo: RepoMap, max_tokens: int, include_private: bool) -> str:
    """Files in rank order until the character budget (tokens * 4) is used, then a footer."""
    budget = max_tokens * 4
    blocks: list[str] = []
    used = 0
    for info in repo.files:
        defs = [d for d in info.definitions if include_private or not d.name.startswith("_")]
        if not defs:
            continue
        block = "\n".join([info.rel, *(f"│ {'  ' * d.depth}{d.signature}" for d in defs)])
        if used + len(block) + 1 > budget:
            break
        blocks.append(block)
        used += len(block) + 1
    footer = (
        f"[repo_map: {len(blocks)} of {len(repo.files)} files shown within {max_tokens} tokens; "
        f"{repo.unsupported} files in unsupported languages]"
    )
    return "\n".join([*blocks, footer])


def load_cache(root: Path) -> dict[str, Any]:
    """The cache file's content, or an empty cache."""
    try:
        data = json.loads((root / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(root: Path, cache: dict[str, Any]) -> None:
    """Write the cache; failures only cost speed next time."""
    path = root / CACHE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
