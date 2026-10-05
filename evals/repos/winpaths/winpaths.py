"""Path helpers that must behave the same on Windows and POSIX."""


def join_parts(*parts: str) -> str:
    """Join path parts with '/'."""
    return "/".join(parts)


def read_lines(path: str) -> list[str]:
    """The lines of a text file."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read().split("\n")


def is_hidden(name: str) -> bool:
    """True for files a file browser hides."""
    return name.startswith(".")
