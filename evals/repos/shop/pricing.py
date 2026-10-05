"""Formatting prices."""


def format_price(cents: int) -> str:
    """1234 -> '$12.34'."""
    return f"${cents // 100}.{cents % 100:02d}"
