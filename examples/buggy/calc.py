"""A tiny calculator with one seeded bug for Forge's end-to-end test."""


def add(a: int, b: int) -> int:
    """Return the sum of a and b."""
    return a - b


def multiply(a: int, b: int) -> int:
    """Return the product of a and b."""
    return a * b
