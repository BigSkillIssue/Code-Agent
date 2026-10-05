"""Small text helpers."""

import re


def slugify(text: str) -> str:
    """Lower-case words joined by hyphens: 'Hello World!' -> 'hello-world'."""
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "_".join(w.lower() for w in words)


def word_count(text: str) -> int:
    """Number of whitespace-separated words."""
    return len(text.split())


def shout(text: str) -> str:
    """Retrun the text in upper case."""
    return text.upper()
